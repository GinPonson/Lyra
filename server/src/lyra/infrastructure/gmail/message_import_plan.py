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
from collections.abc import Mapping
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
from ...domain.fifo.replay import assert_replay_order_is_provable
from ...domain.fifo.types import FifoError
from ...domain.gmail.econfirm import (
    EmailTrade,
    GmailMessage,
    UnresolvedCashOptionSaleError,
    email_trade_to_raw_fields,
)
from ...domain.ledger.batch import TransactionBatchInput
from ...domain.ledger.parsers import parse_date_field, parse_option_symbol
from ...domain.ledger.transactions import (
    create_business_hash,
    decimal_hash_text,
    hash_text,
    with_occurrence,
)
from ...domain.ledger.types import ParsedTransaction
from ...domain.ledger.types import RawTransactionFields
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
    existing_gmail_occurrence_maxima: Mapping[str, int] | None = None,
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
    @param existing_gmail_occurrence_maxima: 已有 Gmail 关联中每个业务哈希的最大出现次数；
        用于让跨邮件 occurrence 接续，而不改通用交易哈希规则。
    @returns 跳过或就绪的计划。
    @raises ApplicationError: 同一消息的内容或账户归属发生变化（409）。
    @raises UnresolvedCashOptionSaleError: 只可关联已有成交的 Cash 期权行试图新增交易。
    """
    metadata = create_parsed_gmail_message_metadata(
        connection_id=connection_id, thread_id=thread_id, message=message,
        trade_count=len(trades), account_id=account_id, history_id=history_id,
        synced_at=synced_at,
    )

    if existing_message is not None and existing_message.parse_status == "imported":
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
        return AlreadyImportedGmailMessagePlan(
            kind="already_imported",
            connection_id=metadata.connection_id,
            message_id=metadata.message_id,
            content_hash=metadata.content_hash,
        )
    # 未成功的状态允许用当前内容与映射重试；失败记录上的空账户不代表已锁定归属。

    raw_rows = tuple(email_trade_to_raw_fields(trade) for trade in trades)  # type: ignore[arg-type]
    raw_rows = _align_existing_transaction_facts(
        raw_rows, existing_account_transactions, ledger_account
    )
    existing_hashes = frozenset(
        transaction.transaction_hash for transaction in existing_account_transactions
    )
    preview = create_import_preview(CreateImportPreviewInput(
        batch=TransactionBatchInput(
            file_name=f"gmail:{metadata.message_id}",
            account=ledger_account,
            source_hash=_gmail_message_source_hash(metadata),
            source_type="gmail",
            source_reference=metadata.message_id,
            rows=raw_rows,
        ),
        parser_version=parser_version,
        base_ledger_version=base_ledger_version,
        existing_transaction_hashes=existing_hashes,
    ))
    transactions = _continue_gmail_occurrences(
        preview.transactions, existing_gmail_occurrence_maxima or {}
    )
    existing_hashes = frozenset(
        transaction.transaction_hash for transaction in existing_account_transactions
    )
    if any(
        trade.existing_only and transaction.transaction_hash not in existing_hashes
        for trade, transaction in zip(trades, transactions, strict=True)
    ):
        # Cash 本身没有期权开平仓信息；既有成交只证明可关联的 occurrence，不能借它
        # 为另一笔相似邮件创造新动作。整个消息在提交前拒绝，避免部分腿误入账。
        raise UnresolvedCashOptionSaleError(
            "期权 Sale/Cash 缺少开平仓信息，且没有足够的已确认成交可关联；该邮件未入账。"
        )
    insertions = tuple(
        transaction for transaction in transactions
        if transaction.transaction_hash not in existing_hashes
    )
    if insertions:
        _assert_gmail_replay_order_is_provable(
            (*existing_account_transactions, *insertions)
        )
    commit = build_import_commit_plan(
        ImportCommitPreview(account=preview.account, transactions=transactions),
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
            for index, transaction in enumerate(transactions)
        ),
    )


_BERKSHIRE_CLASS_B_STOCK_ALIAS = "berkshire_class_b"


def _recognized_stock_alias(symbol: str | None, description: str | None) -> str | None:
    """识别唯一已核实的邮件与 CSV 股票代码表示差异。

    @param symbol: Schwab 原始股票代码。
    @param description: 原始证券描述。
    @returns 仅当代码和发行人/股类描述同时精确匹配已确认组合时返回别名键。
    """
    if symbol is None or description is None:
        return None
    normalized_symbol = symbol.strip().upper()
    normalized_description = " ".join(description.split()).upper()
    if (normalized_symbol, normalized_description) in {
        ("BRK.B", "BERKSHIRE HATHAWAY B"),
        ("BRK.B", "BERKSHIRE HATHAWAY CLASS B"),
        ("BRKB", "BERKSHIRE HATHAWAY CLASS B"),
        # 嘉信既有流水也会重复 CLASS；只接受已核实的完整表示，不通用改写描述。
        ("BRKB", "BERKSHIRE HATHAWAY CLASS CLASS B"),
    }:
        return _BERKSHIRE_CLASS_B_STOCK_ALIAS
    return None


def _format_schwab_date_field(record_date: str, transaction_date: str) -> str:
    """把 ISO 日期还原为 Schwab 日期列，保留 ``as of`` 两个日期语义。

    @param record_date: 账本记录日期，格式为 ``YYYY-MM-DD``。
    @param transaction_date: 实际成交日期，格式为 ``YYYY-MM-DD``。
    @returns ``MM/DD/YYYY`` 或 ``MM/DD/YYYY as of MM/DD/YYYY``。
    @raises ValueError: 任一日期不符合标准 ISO 日期格式。
    """
    def format_date(value: str) -> str:
        """格式化一个已标准化的 ISO 日历日期。"""
        parts = value.split("-")
        if len(parts) != 3 or any(not part.isdigit() for part in parts):
            raise ValueError("账本成交日期格式无效")
        year, month, day = parts
        if len(year) != 4 or len(month) != 2 or len(day) != 2:
            raise ValueError("账本成交日期格式无效")
        return f"{month}/{day}/{year}"

    formatted_record_date = format_date(record_date)
    formatted_transaction_date = format_date(transaction_date)
    if record_date == transaction_date:
        return formatted_record_date
    return f"{formatted_record_date} as of {formatted_transaction_date}"


def _align_existing_transaction_facts(
    rows: tuple[RawTransactionFields, ...],
    existing_transactions: tuple[ParsedTransaction, ...],
    ledger_account: str,
) -> tuple[RawTransactionFields, ...]:
    """只在全部成交事实精确一致时对齐既有证券表示、描述和记录日期。

    Description 与记录日期可能因邮件和 CSV 的展示方式不同。这里保留账户、完整证券
    身份、实际成交日、动作以及所有原始数值文本的哈希精度；只有重建出唯一既有业务
    哈希时才复用该行的描述和记录日期。股票符号表示只接受明确配置的 Berkshire B 类
    股组合，其他符号和描述不会做通用别名或模糊匹配。

    @param rows: eConfirm 转换出的原始成交行。
    @param existing_transactions: 目标账户已有交易快照。
    @param ledger_account: 用于业务哈希的 Schwab 账户键。
    @returns 对齐唯一既有成交事实后的原始行；新成交保留邮件描述与数值。
    @raises ApplicationError: 账户含多个已确认别名表示或字段匹配多个业务哈希（409）。
    """
    aligned: list[RawTransactionFields] = []
    for row in rows:
        row_option = parse_option_symbol(row.symbol or "")
        row_stock_alias = (
            _recognized_stock_alias(row.symbol, row.description)
            if row_option is None
            else None
        )
        alias_symbols = {
            transaction.raw_symbol
            for transaction in existing_transactions
            if row_stock_alias is not None
            and transaction.account == ledger_account
            and transaction.asset_type == "stock"
            and transaction.raw_symbol is not None
            and transaction.underlying == transaction.raw_symbol
            and _recognized_stock_alias(
                transaction.raw_symbol, transaction.description
            ) == row_stock_alias
        }
        if len(alias_symbols) > 1:
            raise ApplicationError(
                code="GMAIL_REPLAY_ORDER_AMBIGUOUS",
                message="账户中存在多种已确认股票代码表示，无法确认证券身份，请检查嘉信 CSV。",
                status_code=409,
                retryable=False,
                details={"reason": "stock_alias_representation_ambiguous"},
            )

        # 新别名成交沿用账户当前唯一使用的代码，避免同一证券被 FIFO 拆成两本持仓。
        row_symbol = next(iter(alias_symbols)) if alias_symbols else row.symbol
        parsed_date = parse_date_field(row.date)
        matching_transactions: dict[str, ParsedTransaction] = {}
        for transaction in existing_transactions:
            if transaction.account != ledger_account:
                continue
            if transaction.transaction_date != parsed_date.transaction_date:
                continue
            if transaction.action != row.action:
                continue

            if row_option is not None:
                existing_option = parse_option_symbol(transaction.raw_symbol or "")
                if (
                    transaction.asset_type != "option"
                    or existing_option != row_option
                    or transaction.raw_symbol != row_symbol
                    or transaction.underlying != row_option.underlying
                    or transaction.expiry != row_option.expiry
                    or transaction.strike != row_option.strike
                    or transaction.option_type != row_option.option_type
                ):
                    continue
            elif row_stock_alias is not None:
                if (
                    transaction.asset_type != "stock"
                    or transaction.raw_symbol != row_symbol
                    or transaction.underlying != row_symbol
                    or transaction.expiry is not None
                    or transaction.strike is not None
                    or transaction.option_type is not None
                    or _recognized_stock_alias(
                        transaction.raw_symbol, transaction.description
                    ) != row_stock_alias
                ):
                    continue
            elif (
                transaction.asset_type != "stock"
                or transaction.raw_symbol != row_symbol
                or transaction.underlying != row_symbol
                or transaction.expiry is not None
                or transaction.strike is not None
                or transaction.option_type is not None
            ):
                continue

            candidate_hash = create_business_hash(
                account=transaction.account,
                record_date=transaction.record_date,
                transaction_date=transaction.transaction_date,
                action=row.action,
                raw_symbol=row_symbol,
                description=transaction.description,
                quantity_text=decimal_hash_text(row.quantity),
                price_text=decimal_hash_text(row.price),
                fees_text=decimal_hash_text(row.fees),
                amount_text=decimal_hash_text(row.amount),
            )
            if candidate_hash == transaction.business_hash:
                matching_transactions.setdefault(
                    transaction.business_hash, transaction
                )

        if len(matching_transactions) > 1:
            raise ApplicationError(
                code="GMAIL_REPLAY_ORDER_AMBIGUOUS",
                message="同日成交顺序无法确认，请导入包含该日全部成交的嘉信 CSV。",
                status_code=409,
                retryable=False,
                details={"reason": "transaction_fact_match_ambiguous"},
            )
        if not matching_transactions:
            if row_symbol == row.symbol:
                aligned.append(row)
            else:
                aligned.append(RawTransactionFields(
                    date=row.date,
                    action=row.action,
                    symbol=row_symbol or "",
                    description=row.description,
                    quantity=row.quantity,
                    price=row.price,
                    fees=row.fees,
                    amount=row.amount,
                ))
            continue

        transaction = next(iter(matching_transactions.values()))
        aligned.append(RawTransactionFields(
            date=_format_schwab_date_field(
                transaction.record_date, transaction.transaction_date
            ),
            action=row.action,
            symbol=transaction.raw_symbol or row_symbol or "",
            description=transaction.description or "",
            quantity=row.quantity,
            price=row.price,
            fees=row.fees,
            amount=row.amount,
        ))
    return tuple(aligned)


def _continue_gmail_occurrences(
    transactions: tuple[ParsedTransaction, ...],
    existing_maxima: Mapping[str, int],
) -> tuple[ParsedTransaction, ...]:
    """把本封邮件内的相同业务 occurrence 接续到已关联的 Gmail 成交之后。

    @param transactions: 本封邮件按原文顺序标准化后的交易。
    @param existing_maxima: 每个 business_hash 已关联 occurrence 的最大值。
    @returns 交易字段不变、仅 occurrence 与 transaction_hash 更新后的序列。
    @raises ValueError: 已保存的 occurrence 最大值无效。
    """
    counts: dict[str, int] = {}
    continued: list[ParsedTransaction] = []
    for transaction in transactions:
        local_count = counts.get(transaction.business_hash, 0) + 1
        counts[transaction.business_hash] = local_count
        previous_maximum = existing_maxima.get(transaction.business_hash, 0)
        if not isinstance(previous_maximum, int) or isinstance(previous_maximum, bool) or previous_maximum < 0:
            raise ValueError("已有 Gmail occurrence 最大值无效")
        continued.append(with_occurrence(transaction, previous_maximum + local_count))
    return tuple(continued)


def _gmail_message_source_hash(metadata: ParsedGmailMessageMetadata) -> str:
    """为每封 Gmail 消息建立独立、可复现的 FIFO 来源身份。

    @param metadata: 已完成内容摘要与 message ID 规范化的消息元数据。
    @returns 包含连接、消息与内容身份的 SHA-256 摘要。
    """
    return hash_text(
        f"gmail|{metadata.connection_id}|{metadata.message_id}|{metadata.content_hash}"
    )


def _assert_gmail_replay_order_is_provable(
    transactions: tuple[ParsedTransaction, ...],
) -> None:
    """把 FIFO 的同日多来源顺序歧义转成 Gmail 可识别的金融冲突。

    ``assert_replay_order_is_provable`` 只检测来源顺序歧义；其余 FIFO 错误仍由提交计划
    原样抛出，避免把超卖等账本问题误报成邮件顺序冲突。

    @param transactions: 当前账户与本封邮件拟新增的交易。
    @raises ApplicationError: FIFO 顺序无法证明（409）。
    """
    try:
        assert_replay_order_is_provable(transactions)
    except FifoError as error:
        raise ApplicationError(
            code="GMAIL_REPLAY_ORDER_AMBIGUOUS",
            message="同日成交顺序无法确认，请导入包含该日全部成交的嘉信 CSV。",
            status_code=409,
            retryable=False,
        ) from error


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

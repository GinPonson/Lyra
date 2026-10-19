"""Gmail 消息处理循环。

镜像 TypeScript 版 ``gmail-sync-service.ts`` 的 ``#processMessages``。

**四种结局各自计数，互不混淆**：

| 结局 | 含义 | 用户的下一步 |
| --- | --- | --- |
| `imported` | 交易已入账 | 无 |
| `alreadyImported` | 这封邮件此前已导入且未变 | 无 |
| `conflict` | 账户尾号需要用户确认映射 | 选择账户 |
| `rejected` | 邮件内容解析失败 | 检查邮件（或忽略） |

**把 conflict 与 rejected 合并会让用户无从下手**：前者要点一下选账户，后者要看邮件
内容，是两个完全不同的动作。

**单封邮件失败不终止整次同步**：一封格式异常的邮件不该让同一批里的其它邮件都不被
导入——那会让用户反复重试且每次都停在同一封上。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..domain.gmail.econfirm import (
    UnresolvedCashOptionSaleError,
    extract_account_suffix,
    parse_econfirm,
)
from ..domain.ledger.errors import IngestError
from .app_error import ApplicationError


def _default_parse_trades(message: Any, explicit_account_id: str | None) -> tuple:
    """默认解析邮件交易。

    @param message: 已水合的嘉信成交确认邮件。
    @param explicit_account_id: 用户本次显式选中的账户 UUID；默认解析不需要它。
    @returns 规范化的邮件交易序列。
    @raises IngestError: 邮件正文不是受支持的成交确认。
    """
    del explicit_account_id
    return parse_econfirm(message)


@dataclass
class GmailMessageProcessSummary:
    """处理结果计数。"""

    imported_messages: int = 0
    already_imported_messages: int = 0
    conflict_messages: int = 0
    rejected_messages: int = 0
    inserted_transactions: int = 0
    existing_transactions: int = 0
    replay_order_conflict_messages: int = 0

    def to_dict(self) -> dict[str, int]:
        """投影为公开的 camelCase 字段。"""
        return {
            "importedMessages": self.imported_messages,
            "alreadyImportedMessages": self.already_imported_messages,
            "conflictMessages": self.conflict_messages,
            "rejectedMessages": self.rejected_messages,
            "insertedTransactions": self.inserted_transactions,
            "existingTransactions": self.existing_transactions,
            "replayOrderConflictMessages": self.replay_order_conflict_messages,
        }


@dataclass(frozen=True)
class GmailMessageImportOutcome:
    """区分实际入账笔数与已处理邮件，避免将 CSV 重叠误报为新增交易。

    @param kind: 本次处理的新邮件，或此前已归档且内容不变的邮件。
    @param inserted_transactions: 实际新增的账本行数，非负整数。
    @param existing_transactions: 已与现有事实匹配的行数，非负整数。
    @raises ValueError: 状态或计数无效，或已归档邮件声称新增交易。
    """

    kind: Literal["imported", "already_imported"]
    inserted_transactions: int
    existing_transactions: int

    def __post_init__(self) -> None:
        """在投影成功结果前拒绝无效计数与自相矛盾的已归档状态。"""
        if self.kind not in ("imported", "already_imported") or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (self.inserted_transactions, self.existing_transactions)
        ):
            raise ValueError("Gmail 导入结果包含无效状态或交易计数")
        if self.kind == "already_imported" and self.inserted_transactions:
            raise ValueError("已归档的 Gmail 消息不能声称新增交易")


@dataclass(frozen=True)
class GmailMessageProcessingDependencies:
    """处理循环所需的外部动作。"""

    # (record) -> 邮件序列
    parse_records: Callable[[Any], tuple[Any, ...]]
    # () -> 全部账户
    load_accounts: Callable[[], tuple[Any, ...]]
    # () -> 该连接的映射行
    load_mappings: Callable[[], tuple[Any, ...]]
    # (suffix, accounts, existing_mapping, explicit_account_id) -> 解析结果
    resolve_mapping: Callable[..., Any]
    # (mapping_to_create) -> None
    create_mapping: Callable[[Any], None]
    # (message, error_code) -> None
    persist_unsuccessful: Callable[[Any, str], None]
    # 实际提交结果用于区分 CSV 重叠与新增事实，不能以处理封数代替金融写入数。
    import_message: Callable[[Any, tuple, Any], GmailMessageImportOutcome]
    # (message, explicit_account_id) -> 解析交易；默认仍只调用原始领域解析器。
    parse_trades: Callable[[Any, str | None], tuple] = _default_parse_trades


def process_gmail_messages(
    records: tuple[Any, ...],
    *,
    dependencies: GmailMessageProcessingDependencies,
    explicit_account_id: str | None = None,
) -> GmailMessageProcessSummary:
    """处理一批 Gmail 记录。

    @param records: 完整消息记录（已水合正文）。
    @param dependencies: 外部动作集合。
    @param explicit_account_id: 用户显式指定的账户。
    @returns 消息各类结局、顺序冲突以及实际新增 / 已有交易计数。
    @raises ApplicationError: 未归类为账户映射或成交顺序冲突的真实业务错误。
    """
    summary = GmailMessageProcessSummary()
    accounts = dependencies.load_accounts()
    candidates: list[tuple[Any, tuple]] = []

    for record in records:
        for message in dependencies.parse_records(record):
            try:
                trades = dependencies.parse_trades(message, explicit_account_id)
            except ApplicationError as error:
                if error.code != "ACCOUNT_MAPPING_REQUIRED":
                    # 只有账户选择冲突可以归档并继续；其它业务错误必须暴露给调用方。
                    raise
                dependencies.persist_unsuccessful(message, "ACCOUNT_MAPPING_REQUIRED")
                summary.conflict_messages += 1
                continue
            except IngestError:
                # 内容解析失败：记一笔 rejected 并继续下一封。
                dependencies.persist_unsuccessful(message, "GMAIL_MESSAGE_REJECTED")
                summary.rejected_messages += 1
                continue
            candidates.append((message, trades))

    # Gmail 通常倒序返回消息。先处理较早交易日，避免逐封提交期间先出现缺少前序买入的卖出。
    # 同日先后仍交给金融内核证明；这里不会把收信时间或 message ID 当作成交顺序。
    candidates.sort(key=lambda item: min(trade.trade_date for trade in item[1]))
    for message, trades in candidates:
        suffix = extract_account_suffix(message)
        existing = _find_mapping(dependencies.load_mappings(), suffix)
        try:
            mapping = dependencies.resolve_mapping(
                suffix=suffix, accounts=accounts,
                existing_mapping=existing,
                explicit_account_id=explicit_account_id,
            )
        except ApplicationError as error:
            if error.code != "ACCOUNT_MAPPING_REQUIRED":
                # 其它错误是真正的故障，不吞掉。
                raise
            dependencies.persist_unsuccessful(message, "ACCOUNT_MAPPING_REQUIRED")
            summary.conflict_messages += 1
            continue

        if mapping.mapping_to_create is not None:
            dependencies.create_mapping(mapping.mapping_to_create)
        try:
            outcome = dependencies.import_message(message, trades, mapping)
        except UnresolvedCashOptionSaleError:
            # 导入计划发现 existing_only 成交无法关联既有事实时，明确拒绝整封邮件。
            dependencies.persist_unsuccessful(message, "GMAIL_MESSAGE_REJECTED")
            summary.rejected_messages += 1
            continue
        except ApplicationError as error:
            if error.code != "GMAIL_REPLAY_ORDER_AMBIGUOUS":
                raise
            dependencies.persist_unsuccessful(message, error.code)
            summary.conflict_messages += 1
            summary.replay_order_conflict_messages += 1
            continue
        summary.inserted_transactions += outcome.inserted_transactions
        summary.existing_transactions += outcome.existing_transactions
        if outcome.kind == "already_imported":
            summary.already_imported_messages += 1
        else:
            summary.imported_messages += 1

    return summary


def _find_mapping(mappings: tuple[Any, ...], suffix: str) -> Any:
    """按**大写化**的尾号在已有映射里查找。

    与参考实现一致：尾号的规范化只做大小写，不做别的推断。
    """
    for mapping in mappings:
        if mapping.account_suffix.strip().upper() == suffix:
            return mapping
    return None

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
from typing import Any

from ..domain.gmail.econfirm import extract_account_suffix, parse_econfirm
from ..domain.ledger.errors import IngestError
from .app_error import ApplicationError


@dataclass
class GmailMessageProcessSummary:
    """处理结果计数。"""

    imported_messages: int = 0
    already_imported_messages: int = 0
    conflict_messages: int = 0
    rejected_messages: int = 0

    def to_dict(self) -> dict[str, int]:
        """投影为公开的 camelCase 字段。"""
        return {
            "importedMessages": self.imported_messages,
            "alreadyImportedMessages": self.already_imported_messages,
            "conflictMessages": self.conflict_messages,
            "rejectedMessages": self.rejected_messages,
        }


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
    # (message, trades, mapping) -> "imported" | "already_imported"
    import_message: Callable[[Any, tuple, Any], str]


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
    @returns 四种结局的计数。
    @raises ApplicationError: 映射解析抛出的**非** ``ACCOUNT_MAPPING_REQUIRED`` 错误。
    """
    summary = GmailMessageProcessSummary()
    accounts = dependencies.load_accounts()

    for record in records:
        for message in dependencies.parse_records(record):
            try:
                trades = parse_econfirm(message)
            except IngestError:
                # 内容解析失败：记一笔 rejected 并继续下一封。
                dependencies.persist_unsuccessful(message, "GMAIL_MESSAGE_REJECTED")
                summary.rejected_messages += 1
                continue

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

            outcome = dependencies.import_message(message, trades, mapping)
            if outcome == "already_imported":
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

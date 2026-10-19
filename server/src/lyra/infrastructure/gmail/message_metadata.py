"""Gmail 消息的持久化元数据。

镜像 TypeScript 版 ``infrastructure/gmail/message-metadata.ts``。

**内容摘要做三处规范化**：

```
{sender: 去空白 + 小写, subject: 去空白, body: CRLF → LF}
```

少了任何一处，同一封邮件重新抓取都会被判定为"内容变了"——换行符差异尤其常见，
因为 Gmail API 与原始 RFC 邮件的换行表示不同。误判会触发一次不必要的冲突处理，
而用户看到的是"这封邮件有冲突"。

**失败记录不得包含正文**：稳定错误码足以让 App 展示后续动作，而把正文写进失败记录
等于把解析不了的原始内容留在库里。摘要仍与成功路径一致，使同一 message ID 的内容
变化可以被检测。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

from ...domain.gmail.econfirm import GmailMessage
from ...domain.gmail.sync_state import normalize_gmail_history_id
from ...domain.ledger.errors import IngestError

ParseStatus = Literal["parsed", "rejected", "conflict"]
UnsuccessfulErrorCode = Literal[
    "GMAIL_MESSAGE_REJECTED", "ACCOUNT_MAPPING_REQUIRED", "GMAIL_MESSAGE_CONFLICT",
    "GMAIL_REPLAY_ORDER_AMBIGUOUS",
]

_EPOCH = re.compile(r"^\d+$")


@dataclass(frozen=True)
class ParsedGmailMessageMetadata:
    """成功解析的消息元数据。"""

    connection_id: str
    message_id: str
    thread_id: str | None
    received_at: str | None
    content_hash: str
    account_id: str | None
    parse_status: ParseStatus
    error_code: str | None
    transaction_count: int
    history_id: str | None
    synced_at: str


def create_parsed_gmail_message_metadata(
    *,
    connection_id: str,
    thread_id: str | None,
    message: GmailMessage,
    trade_count: int,
    account_id: str | None,
    history_id: str | None,
    synced_at: datetime,
) -> ParsedGmailMessageMetadata:
    """为成功解析的消息建立元数据。

    @param connection_id: 连接 ID。
    @param thread_id: 会话 ID；可为空。
    @param message: 已解析的邮件。
    @param trade_count: 提取到的成交笔数。
    @param account_id: 映射到的账户；可为空。
    @param history_id: 同步游标；可为空。
    @param synced_at: 服务端可信时间。
    @returns 元数据。
    """
    base = _base_metadata(
        connection_id=connection_id, thread_id=thread_id, message=message,
        history_id=history_id, synced_at=synced_at,
    )
    return ParsedGmailMessageMetadata(
        **base,
        account_id=_nullable_text(account_id),
        parse_status="parsed",
        error_code=None,
        transaction_count=trade_count,
    )


def create_unsuccessful_gmail_message_metadata(
    *,
    connection_id: str,
    thread_id: str | None,
    message: GmailMessage,
    account_id: str | None,
    history_id: str | None,
    synced_at: datetime,
    error_code: UnsuccessfulErrorCode,
) -> ParsedGmailMessageMetadata:
    """为拒绝或冲突建立**无正文**的持久化记录。

    @param error_code: 稳定错误码。
    @returns 元数据；``parse_status`` 由错误码决定。
    """
    base = _base_metadata(
        connection_id=connection_id, thread_id=thread_id, message=message,
        history_id=history_id, synced_at=synced_at,
    )
    return ParsedGmailMessageMetadata(
        **base,
        account_id=_nullable_text(account_id),
        # 拒绝与冲突是两类不同的后续动作：前者看邮件内容，后者看账户映射。
        parse_status="rejected" if error_code == "GMAIL_MESSAGE_REJECTED" else "conflict",
        error_code=error_code,
        transaction_count=0,
    )


def _base_metadata(
    *,
    connection_id: str,
    thread_id: str | None,
    message: GmailMessage,
    history_id: str | None,
    synced_at: datetime,
) -> dict:
    """成功与失败元数据共用完全相同的身份、时间与内容摘要规则。"""
    # 三处规范化缺一不可，否则重抓同一封邮件会被判定为"内容变了"。
    canonical = json.dumps(
        {
            "sender": message.sender.strip().lower(),
            "subject": message.subject.strip(),
            "body": re.sub(r"\r\n?", "\n", message.body),
        },
        separators=(",", ":"), ensure_ascii=False,
    )
    return {
        "connection_id": _require_text(connection_id, "Gmail 连接 ID"),
        "message_id": _require_text(message.message_id, "Gmail message ID"),
        "thread_id": _nullable_text(thread_id),
        "received_at": (
            None if message.email_timestamp is None
            else parse_email_timestamp(message.email_timestamp)
        ),
        "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "history_id": (
            None if history_id is None else normalize_gmail_history_id(history_id)
        ),
        "synced_at": _require_timestamp(synced_at, "Gmail 同步时间"),
    }


def parse_email_timestamp(value: str) -> str:
    """规范邮件时间戳。

    接受**纯数字的毫秒值**或可解析的时间文本（Gmail ``Date`` 头或 ``internalDate``）。

    @param value: 时间戳原文。
    @returns ISO UTC 文本。
    @raises IngestError: 无法解析。
    """
    normalized = value.strip()
    if _EPOCH.fullmatch(normalized):
        milliseconds = int(normalized)
        try:
            return _iso(datetime.fromtimestamp(milliseconds / 1000, tz=UTC))
        except (OverflowError, OSError, ValueError) as error:
            raise IngestError("Gmail 邮件时间戳无效") from error
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(normalized)
        except Exception as error:
            raise IngestError("Gmail 邮件时间戳无效") from error
    if parsed is None:
        raise IngestError("Gmail 邮件时间戳无效")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _iso(parsed)


def _require_text(value: str, label: str) -> str:
    """去除边界空白并拒绝空必填标识。"""
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized


def _nullable_text(value: str | None) -> str | None:
    """规范可空标识，空白等价于数据库 null。"""
    if value is None:
        return None
    normalized = value.strip()
    return None if normalized == "" else normalized


def _require_timestamp(value: datetime, label: str) -> str:
    """校验可信时间并转换为 UTC ISO 文本。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label}无效")
    return _iso(value)


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

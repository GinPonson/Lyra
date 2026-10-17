"""Gmail 同步状态标识。

镜像 TypeScript 版 ``domain/gmail/sync-state.ts`` 的规范化函数。
"""

from __future__ import annotations

import re

_HISTORY_ID = re.compile(r"^[1-9][0-9]*$")


def normalize_gmail_history_id(value: str) -> str:
    """规范 Gmail history id。

    **必须是正十进制字符串且无前导零**：前导零会让同一个游标有两种写法，
    增量同步的游标比较就会误判为"变了"。

    @param value: 游标原文。
    @returns 规范化后的游标。
    @raises ValueError: 不是正十进制字符串。
    """
    normalized = value.strip()
    if not _HISTORY_ID.fullmatch(normalized):
        raise ValueError("Gmail history id 必须是正十进制字符串")
    return normalized


def normalize_gmail_sync_timestamp(value: str) -> str:
    """规范 Gmail 同步完成时间。

    **必须显式以 ``Z`` 结尾**：没有时会话时区会让同一个瞬时在不同机器上产生不同
    文本，而它会进入审计记录。

    @param value: 时间戳原文。
    @returns 规范化后的时间戳。
    @raises ValueError: 不带 UTC 标记或无法解析。
    """
    from datetime import datetime

    normalized = value.strip()
    if not normalized.endswith("Z"):
        raise ValueError("Gmail 同步完成时间必须是 UTC ISO 时间戳")
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Gmail 同步完成时间无效") from error
    return normalized

"""账户标识的解析。

**账本查询用的是"脱敏账户键"（如 ``TST001``），而会话与公开 API 用的是账户 UUID。**
两者类型相同（都是字符串），因此把 UUID 直接当键传给账本查询**不会报错**——
只会匹配不到任何行并返回一个空账本。

这是本项目里最容易静默出错的一类转换，因此单独成模块并给出明确命名。
"""

from __future__ import annotations

import duckdb


def resolve_legacy_account_key(
    connection: duckdb.DuckDBPyConnection, account_id: str
) -> str | None:
    """把账户 UUID 解析为账本用的脱敏账户键。

    @param connection: 数据库连接。
    @param account_id: 账户 UUID。
    @returns 脱敏账户键；账户不存在时 ``None``。
    """
    row = connection.execute(
        "SELECT legacy_account_key FROM accounts WHERE id = CAST(? AS UUID)",
        [account_id],
    ).fetchone()
    return None if row is None else str(row[0])

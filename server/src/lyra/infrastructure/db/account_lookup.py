"""账户标识的解析。

**账本查询用的是"脱敏账户键"（如 ``TST001``），而会话与公开 API 用的是账户 UUID。**
两者类型相同（都是字符串），因此把 UUID 直接当键传给账本查询**不会报错**——
只会匹配不到任何行并返回一个空账本。

这是本项目里最容易静默出错的一类转换，因此单独成模块并给出明确命名。

多用户下还叠加了第二重静默风险：账户 UUID **不区分用户**。解析必须同时限定
``user_id``，否则解析出的是别人的账户键（见 :func:`resolve_legacy_account_key`）。
"""

from __future__ import annotations

import duckdb


def resolve_legacy_account_key(
    connection: duckdb.DuckDBPyConnection, user_id: str, account_id: str
) -> str | None:
    """把账户 UUID 解析为账本用的脱敏账户键。

    **账户标识已在用户内唯一，因此解析必须带用户范围**（P0 契约 §3.11）：不带范围时
    A 传入 B 的账户 UUID 会解析出 B 的 legacy key，账本查询随后就会返回 B 的数据——
    这是一条真实的越权入口，而不是理论风险。

    ``user_id`` 刻意放在 ``account_id`` **之前**：两个参数都是字符串，顺序写反不会
    报错，只会静默解析到别人的账户；把用户放在前面能让漏传时直接缺参报错。

    不属于该用户的账户与不存在的账户返回**同一个 ``None``**，调用方据此返回同一个
    404——区分两者会让攻击者靠状态码枚举出他人账户是否存在。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param account_id: 账户 UUID。
    @returns 脱敏账户键；账户不存在或不属于该用户时 ``None``。
    """
    row = connection.execute(
        """
        SELECT legacy_account_key FROM accounts
        WHERE user_id = CAST(? AS UUID) AND id = CAST(? AS UUID)
        """,
        [user_id, account_id],
    ).fetchone()
    return None if row is None else str(row[0])

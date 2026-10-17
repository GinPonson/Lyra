"""Gmail 路径的用户活动校验。

多用户下「这个用户现在还有权写数据吗」是一个**每次写入都要重新问一遍**的问题：
停用操作与业务写入经同一把单写锁排序（计划 §4.4），但一次 Gmail 同步是长流程
（刷新令牌、拉取消息、逐封入账），它可能在请求进入时用户还是 active，而在拉取期间
被停用。**入队时检查一次是不够的**——必须在真正落库前再检查一次。

本模块提供唯一入口 :func:`assert_gmail_user_active`，供 Gmail 的仓储写入与同步
依赖共用：判定只有一处，不会出现"某个调用点忘了查"的分叉。

错误码复用 P2 的 ``USER_DISABLED``（403，``retryable=False``，契约 §3.8）：停用是
授权被撤回，不是凭据失效，客户端应当结束会话而不是进入刷新重试循环。
"""

from __future__ import annotations

import duckdb

from ..infrastructure.db.user_repository import UserRepository
from .device_authenticator import user_disabled_error


def assert_gmail_user_active(
    connection: duckdb.DuckDBPyConnection, user_id: str
) -> None:
    """确认用户仍然存在且处于 ``active``，否则拒绝本次 Gmail 写入。

    **读不到用户与已停用视为同一种拒绝**：两种情况都不该继续写数据，区分它们只会
    让调用方多一条无意义的分支。

    @param connection: 数据库连接。
    @param user_id: 待确认的用户 UUID 文本。
    @raises ApplicationError: 用户不存在或已停用（403，``retryable=False``）。
    """
    user = UserRepository(connection).find_user_by_id(user_id)
    if user is None or user.status != "active":
        raise user_disabled_error()

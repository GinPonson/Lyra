"""Gmail 同步与浏览器 OAuth 的用户/设备活动校验。

多用户下「这个用户现在还有权写数据吗」是一个**每次写入都要重新问一遍**的问题：
停用操作与业务写入经同一把单写锁排序（计划 §4.4），但一次 Gmail 同步是长流程
（刷新令牌、拉取消息、逐封入账），它可能在请求进入时用户还是 active，而在拉取期间
被停用。**入队时检查一次是不够的**——必须在真正落库前再检查一次。

同步依赖使用 :func:`assert_gmail_user_active`；未鉴权的浏览器 OAuth 回调使用
:func:`assert_gmail_authorization_active`，从服务器保存的事务绑定重查用户与设备。
两种入口分别覆盖同步写入与授权等待期间的撤销竞态。

停用用户返回 ``USER_DISABLED``（403，``retryable=False``）；失效或撤销设备返回
``DEVICE_CREDENTIAL_INVALID``（401）。
"""

from __future__ import annotations

import duckdb

from .app_error import ApplicationError
from ..infrastructure.db.device_repository import DuckDbDeviceRepository
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


def assert_gmail_authorization_active(
    connection: duckdb.DuckDBPyConnection, user_id: str, device_id: str
) -> None:
    """确认发起 Gmail OAuth 的设备仍活动且仍属于活动用户。

    浏览器回调没有设备访问令牌，因此不能在回调请求上复用 ``require_user``；这里用
    authorization transaction 中服务器保存的 user/device 绑定重查持久化会话。

    @param connection: 当前数据库连接；可处于 upsert 事务中。
    @param user_id: 创建授权事务的用户 UUID 文本。
    @param device_id: 创建授权事务的设备 UUID 文本。
    @raises ApplicationError: 用户已停用（403）或设备无效/撤销/归属变化（401）。
    """
    repository = DuckDbDeviceRepository(connection)
    device = repository.load_paired_device_auth(device_id)
    user = repository.load_device_user(device_id)
    if user is not None and user.id == user_id and user.status == "disabled":
        raise ApplicationError(
            code="USER_DISABLED",
            message="当前用户已停用",
            status_code=403,
            retryable=False,
        )
    if (
        device is None
        or user is None
        or user.id != user_id
        or user.status != "active"
        or device.status != "active"
        or device.revoked_at is not None
    ):
        raise ApplicationError(
            code="DEVICE_CREDENTIAL_INVALID",
            message="设备访问凭据无效",
            status_code=401,
            retryable=False,
        )

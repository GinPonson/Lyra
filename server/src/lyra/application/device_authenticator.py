"""访问令牌认证。

把「读取访问令牌 → 校验 → 校验用户状态 → 更新活动时间」收敛到一处，供所有业务路由复用。

**读取阶段的失败统一返回 401 且码固定为 ``DEVICE_CREDENTIAL_INVALID``**：
不区分「令牌不存在」「设备不存在」，否则未认证的调用方可以据此枚举历史令牌。
**领域层的失败则保留原始错误码**（与参考实现的认证网关一致）：状态码仍是 401，
但「设备已撤销」「令牌已过期」必须能被 App 区分——压成同一个码会让它分不清
该重新登录还是刷新令牌。

**用户状态必须在这一层拒绝**（``USER_DISABLED``，403）：停用是访问控制决定，不是数据
查询结果。如果留给各业务仓储去过滤，任何一处漏加 ``status = 'active'`` 条件，被停用的
用户就能继续读写账本——而这类遗漏不会在正常路径上暴露。放在认证层意味着**只有一条
判定路径**，路由无法绕过。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ..infrastructure.db.user_repository import UserRecord
from ..security.auth_context import AuthContext
from ..security.device_auth import (
    DeviceAccessTokenRecord,
    DeviceAuthError,
    PairedDeviceAuthRecord,
    authenticate_device_access_token,
    hash_opaque_credential,
    plan_paired_device_activity_update,
)
from .app_error import ApplicationError


class AccessTokenRepository:
    """认证所需的持久化操作。

    实现方只需提供四个方法；认证路径**不写账本**，因此不占用单写队列。
    """

    def load_access_token(self, token_hash: str) -> DeviceAccessTokenRecord | None: ...

    def load_paired_device_auth(self, device_id: str) -> PairedDeviceAuthRecord | None: ...

    def load_device_user(self, device_id: str) -> UserRecord | None: ...

    def touch_access_token(self, token_id: str, now: datetime) -> None: ...


class DeviceAuthenticator:
    """校验 `Authorization: Bearer` 访问令牌并产出用户上下文。"""

    def __init__(
        self,
        repository: AccessTokenRepository,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._now = now or (lambda: datetime.now(UTC))

    def authenticate(self, raw_token: str) -> AuthContext:
        """校验一个明文访问令牌。

        检查顺序与契约一致：**访问令牌 → 设备状态 → 用户状态**。顺序不可调换——
        先查用户会让「令牌根本不存在」的请求也去访问用户表，且改变客户端看到的错误码。

        @param raw_token: Bearer 之后的明文令牌。
        @returns 已认证请求的用户上下文（用户、设备、角色）。
        @raises ApplicationError: 凭据无效（401）；设备撤销/令牌过期保留领域码（401）；
            用户已停用返回 ``USER_DISABLED``（403）。
        """
        if not raw_token:
            raise _unauthorized()
        record = self._repository.load_access_token(hash_opaque_credential(raw_token))
        if record is None:
            raise _unauthorized()
        device = self._repository.load_paired_device_auth(record.device_id)
        if device is None:
            raise _unauthorized()

        now = self._now()
        try:
            updated = authenticate_device_access_token(device, record, raw_token, now)
        except DeviceAuthError as error:
            # **保留原始错误码**：把「设备已撤销」与「令牌已过期」都压成
            # DEVICE_CREDENTIAL_INVALID 会让客户端无法决定是重新登录还是刷新令牌。
            raise ApplicationError(
                code=error.code,
                message=error.message,
                status_code=401,
                retryable=False,
            ) from error

        # 用户是设备的归属方，认证的最后一步是确认它仍然活动（契约 §3.8）。
        user = self._repository.load_device_user(device.id)
        if user is None:
            # 设备行存在却读不到归属用户：数据不一致，按未认证处理，不猜测归属。
            raise _unauthorized()
        if user.status != "active":
            raise user_disabled_error()

        # 活动时间是审计信息，不是鉴权依据：写入失败不应让一次合法请求失败。
        try:
            self._repository.touch_access_token(updated.id, now)
        except Exception:  # noqa: BLE001 - 审计更新失败不阻断请求
            pass

        # 设备活动节流由调用方按需应用；此处只返回最新状态。
        _ = plan_paired_device_activity_update
        return AuthContext(user_id=user.id, device_id=device.id, role=user.role)


def user_disabled_error() -> ApplicationError:
    """「用户已停用」的统一错误。

    403 而不是 401：凭据本身完全有效，是**授权被管理员撤回**。``retryable=False``
    让客户端结束会话而不是进入刷新重试循环——刷新只会拿到同一个 403，把用户卡在
    一个永远失败的循环里（契约 §3.8）。
    """
    return ApplicationError(
        code="USER_DISABLED",
        message="该用户已停用",
        status_code=403,
        retryable=False,
    )


def _unauthorized() -> ApplicationError:
    """统一的未认证错误。"""
    return ApplicationError(
        code="DEVICE_CREDENTIAL_INVALID",
        message="设备访问令牌无效",
        status_code=401,
        retryable=False,
    )

"""访问令牌认证。

把「读取访问令牌 → 校验 → 更新活动时间」收敛到一处，供所有业务路由复用。

**读取阶段的失败统一返回 401 且码固定为 ``DEVICE_CREDENTIAL_INVALID``**：
不区分「令牌不存在」「设备不存在」，否则未认证的调用方可以据此枚举历史令牌。
**领域层的失败则保留原始错误码**（与参考实现的认证网关一致）：状态码仍是 401，
但「设备已撤销」「令牌已过期」必须能被 App 区分——压成同一个码会让它分不清
该重新登录还是刷新令牌。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

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

    实现方只需提供两个方法；认证路径**不写账本**，因此不占用单写队列。
    """

    def load_access_token(self, token_hash: str) -> DeviceAccessTokenRecord | None: ...

    def load_paired_device_auth(self, device_id: str) -> PairedDeviceAuthRecord | None: ...

    def touch_access_token(self, token_id: str, now: datetime) -> None: ...


class DeviceAuthenticator:
    """校验 `Authorization: Bearer` 访问令牌。"""

    def __init__(
        self,
        repository: AccessTokenRepository,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._now = now or (lambda: datetime.now(UTC))

    def authenticate(self, raw_token: str) -> PairedDeviceAuthRecord:
        """校验一个明文访问令牌。

        @param raw_token: Bearer 之后的明文令牌。
        @returns 已认证设备的安全状态。
        @raises ApplicationError: 任意一步失败都返回 401。
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

        # 活动时间是审计信息，不是鉴权依据：写入失败不应让一次合法请求失败。
        try:
            self._repository.touch_access_token(updated.id, now)
        except Exception:  # noqa: BLE001 - 审计更新失败不阻断请求
            pass

        # 设备活动节流由调用方按需应用；此处只返回最新状态。
        _ = plan_paired_device_activity_update
        return device


def _unauthorized() -> ApplicationError:
    """统一的未认证错误。"""
    return ApplicationError(
        code="DEVICE_CREDENTIAL_INVALID",
        message="设备访问令牌无效",
        status_code=401,
        retryable=False,
    )

"""设备会话：OAuth owner 登录后的凭据签发、刷新与撤销。

镜像 TypeScript 版 ``device-pairing-service.ts``。所有签发、刷新与撤销都经
**同一单写队列**，避免刷新与退出登录交错。

与数据库的交互收敛到 :class:`DeviceRepository` 协议：SQL 层的机械实现可以
独立演进，而这里的事务边界、TTL 与安全语义保持不变。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ..infrastructure.auth.login_provider import LoginIdentity
from ..security.device_auth import (
    DeviceAccessTokenRecord,
    DeviceAuthError,
    DeviceRefreshCredentialRecord,
    DeviceRevocationPlan,
    PairedDeviceAuthRecord,
    PairedDeviceRecord,
    RotatedDeviceRefreshCredential,
    create_paired_device_record,
    hash_opaque_credential,
    issue_device_access_token,
    issue_device_refresh_credential,
    normalize_device_public_key,
    plan_refresh_credential_reuse_revocation,
    rotate_device_refresh_credential,
)
from .app_error import ApplicationError
from .write_coordinator import WriteCoordinator

# 短期访问令牌 15 分钟；刷新凭据 30 天。
ACCESS_TOKEN_TTL_MILLISECONDS = 15 * 60 * 1000
REFRESH_TOKEN_TTL_MILLISECONDS = 30 * 24 * 60 * 60 * 1000

# 设备错误码与 HTTP 状态的显式映射，逐项对齐参考实现的 transport 映射表。
#
# **不能一律压成 401**：``DEVICE_REVOKED`` 是 403（已认证但被拒绝），公钥类错误是
# 400（请求本身非法）。客户端按码分支，状态码错了会让它走错分支。
_DEVICE_STATUS_CODES: dict[str, int] = {
    "PAIRING_CODE_INVALID": 400,
    "PAIRING_CODE_EXPIRED": 410,
    "PAIRING_CODE_USED": 409,
    "DEVICE_PUBLIC_KEY_INVALID": 400,
    "DEVICE_CREDENTIAL_INVALID": 401,
    "DEVICE_CREDENTIAL_EXPIRED": 401,
    "DEVICE_CREDENTIAL_REUSED": 401,
    "DEVICE_REVOKED": 403,
}


class DeviceRepository(Protocol):
    """设备会话所需的持久化操作。

    实现方必须保证 :meth:`transaction` 内的全部写入**原子提交**：签发凭据时
    半提交会让 App 拿到一个数据库里不存在的令牌。
    """

    def transaction(self) -> AbstractContextManager[None]: ...

    def claim_server_owner_identity(self, identity: LoginIdentity, now: datetime) -> None: ...

    def find_device_id_by_public_key(self, fingerprint: str) -> str | None: ...

    def create_paired_device(self, record: PairedDeviceRecord) -> None: ...

    def reactivate_paired_device(self, device_id: str, display_name: str, now: datetime) -> None: ...

    def revoke_device_credentials(self, device_id: str, now: datetime) -> None: ...

    def create_refresh_credential(self, record: DeviceRefreshCredentialRecord) -> None: ...

    def create_access_token(self, record: DeviceAccessTokenRecord) -> None: ...

    def load_refresh_credential(self, token_hash: str) -> DeviceRefreshCredentialRecord | None: ...

    def load_paired_device_auth(self, device_id: str) -> PairedDeviceAuthRecord | None: ...

    def load_refresh_credential_family(
        self, device_id: str, family_id: str
    ) -> Sequence[DeviceRefreshCredentialRecord]: ...

    def rotate_refresh_credential(
        self,
        rotation: RotatedDeviceRefreshCredential,
        access_record: DeviceAccessTokenRecord,
    ) -> None: ...

    def apply_family_revocation(self, plan: DeviceRevocationPlan | Any) -> None: ...

    def mark_device_revoked(self, device_id: str, now: datetime) -> None: ...


@dataclass(frozen=True)
class DevicePairingHttpResponse:
    """OAuth 登录成功后 App 获得的 Lyra 凭据，不含任何第三方 token。"""

    device_id: str
    display_name: str
    access_token: str
    access_token_expires_at: str
    refresh_token: str
    refresh_token_expires_at: str


@dataclass(frozen=True)
class DeviceTokenRefreshHttpResponse:
    """刷新成功后返回的新一对凭据。"""

    access_token: str
    access_token_expires_at: str
    refresh_token: str
    refresh_token_expires_at: str


class DevicePairingService:
    """OAuth owner 登录后的设备会话管理。

    保留既有 access/refresh 凭据格式；匿名配对码入口已删除。所有签发、刷新与
    撤销在同一单写队列中执行。
    """

    def __init__(
        self,
        repository: DeviceRepository,
        write_coordinator: WriteCoordinator,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._writes = write_coordinator
        self._now = now or (lambda: datetime.now(UTC))

    async def authorize_owner_device(
        self,
        *,
        identity: LoginIdentity,
        display_name: str,
        public_key_pem: str,
    ) -> DevicePairingHttpResponse:
        """为已验证的 owner 身份签发设备凭据。

        **同一公钥指纹复用设备 ID**：用户重装 App 后用同一把密钥重新登录，
        仍对应同一台设备，而不是每次登录都新增一台。

        重新配对时**先撤销该设备的既有凭据再签发**，避免旧的 refresh 凭据在
        新会话建立后仍然可用。

        @param identity: 已验证的来源身份。
        @param display_name: 用户可识别的设备名称。
        @param public_key_pem: 设备公钥 PEM。
        @returns 设备 ID、名称与一对新凭据。
        @raises ApplicationError: 公钥无效。
        """
        public_key = normalize_device_public_key(public_key_pem)
        return await self._writes.enqueue(lambda: self._authorize(
            identity, display_name, public_key
        ))

    async def _authorize(
        self,
        identity: LoginIdentity,
        display_name: str,
        public_key: Any,
    ) -> DevicePairingHttpResponse:
        """在单写队列内完成一次签发。"""
        now = self._now()
        with self._repository.transaction():
            self._repository.claim_server_owner_identity(identity, now)
            existing_id = self._repository.find_device_id_by_public_key(public_key.fingerprint)
            device_id = existing_id if existing_id is not None else str(uuid.uuid4())
            record = create_paired_device_record(device_id, display_name, public_key, now)
            if existing_id is None:
                self._repository.create_paired_device(record)
            else:
                self._repository.revoke_device_credentials(device_id, now)
                self._repository.reactivate_paired_device(device_id, record.display_name, now)

            auth_record = PairedDeviceAuthRecord(id=device_id, status="active", revoked_at=None)
            refresh = issue_device_refresh_credential(
                device_id, now, REFRESH_TOKEN_TTL_MILLISECONDS
            )
            access = issue_device_access_token(
                auth_record, now, ACCESS_TOKEN_TTL_MILLISECONDS
            )
            self._repository.create_refresh_credential(refresh.record)
            self._repository.create_access_token(access.record)

        return DevicePairingHttpResponse(
            device_id=device_id,
            display_name=record.display_name,
            access_token=access.token,
            access_token_expires_at=access.record.expires_at,
            refresh_token=refresh.token,
            refresh_token_expires_at=refresh.record.expires_at,
        )

    async def refresh_tokens(self, *, refresh_token: str) -> DeviceTokenRefreshHttpResponse:
        """校验并轮换刷新凭据，同时签发新的访问令牌。

        **重用检测会撤销整个凭据族**：旧令牌再次出现说明它已经泄漏，
        只拒绝这一次请求等于把窗口留给攻击者。

        @param refresh_token: App 提交的明文刷新凭据。
        @returns 新的一对凭据。
        @raises ApplicationError: 凭据无效、已撤销、已重用或已过期。
        """
        return await self._writes.enqueue(lambda: self._refresh(refresh_token))

    async def _refresh(self, refresh_token: str) -> DeviceTokenRefreshHttpResponse:
        """在单写队列内完成一次刷新。"""
        now = self._now()
        record = self._repository.load_refresh_credential(hash_opaque_credential(refresh_token))
        if record is None:
            raise self._credential_invalid()
        device = self._repository.load_paired_device_auth(record.device_id)
        if device is None:
            raise self._credential_invalid()
        if record.status != "active":
            self._revoke_credential_family(record)
            raise ApplicationError(
                code="DEVICE_CREDENTIAL_REUSED",
                message="设备刷新凭据已使用或撤销",
                status_code=401,
                retryable=False,
            )
        try:
            rotation = rotate_device_refresh_credential(
                device, record, refresh_token, now, REFRESH_TOKEN_TTL_MILLISECONDS
            )
        except DeviceAuthError as error:
            raise self._device_error(error) from error
        access = issue_device_access_token(device, now, ACCESS_TOKEN_TTL_MILLISECONDS)
        with self._repository.transaction():
            self._repository.rotate_refresh_credential(rotation, access.record)
        return DeviceTokenRefreshHttpResponse(
            access_token=access.token,
            access_token_expires_at=access.record.expires_at,
            refresh_token=rotation.token,
            refresh_token_expires_at=rotation.next.expires_at,
        )

    async def revoke_device(self, device_id: str) -> None:
        """退出登录：撤销设备及其每个 access / refresh 凭据。

        重新登录可复用同一设备 ID。重复调用幂等——设备的撤销时间取
        ``coalesce(revoked_at, now)``，不因重放而改写审计记录。

        @param device_id: 要撤销的设备 ID。
        """
        async def operation() -> None:
            now = self._now()
            with self._repository.transaction():
                self._repository.revoke_device_credentials(device_id, now)
                self._repository.mark_device_revoked(device_id, now)

        await self._writes.enqueue(operation)

    def _revoke_credential_family(self, record: DeviceRefreshCredentialRecord) -> None:
        """撤销整个凭据族（含活动的最新代）。"""
        family = self._repository.load_refresh_credential_family(
            record.device_id, record.family_id
        )
        plan = plan_refresh_credential_reuse_revocation(record, tuple(family), self._now())
        with self._repository.transaction():
            self._repository.apply_family_revocation(plan)

    def _device_error(self, error: DeviceAuthError) -> ApplicationError:
        """把设备领域错误映射为应用错误。

        @param error: 领域层抛出的设备错误。
        @returns 与参考实现 transport 映射表一致的应用错误（状态码按码查表）。
        """
        return ApplicationError(
            code=error.code,
            message=error.message,
            status_code=_DEVICE_STATUS_CODES.get(error.code, 401),
            retryable=False,
        )

    def _credential_invalid(self) -> ApplicationError:
        """统一的凭据无效错误；不区分"不存在"与"不匹配"以防枚举。"""
        return ApplicationError(
            code="DEVICE_CREDENTIAL_INVALID",
            message="设备凭据无效",
            status_code=401,
            retryable=False,
        )

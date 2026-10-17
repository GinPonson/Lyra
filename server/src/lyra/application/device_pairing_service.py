"""设备会话：OAuth 登录后的凭据签发、刷新与撤销。

镜像 TypeScript 版 ``device-pairing-service.ts``。所有签发、刷新与撤销都经
**同一单写队列**，避免刷新与退出登录交错。

与数据库的交互收敛到 :class:`DeviceRepository` 协议：SQL 层的机械实现可以
独立演进，而这里的事务边界、TTL 与安全语义保持不变。

**设备归属用户，且一经归属不再变更**（P0 契约 §3.1/§3.7）：登录时先按已验证身份
解析（或自动注册）用户，再以 ``(user_id, 公钥指纹)`` 复用设备行，因此同一把公钥在
两个用户下是两台互不相干的设备。刷新与认证都必须复核用户状态——停用用户的凭据
即使还没过期也不得继续换取新令牌。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ..infrastructure.auth.login_provider import LoginIdentity
from ..infrastructure.db.user_repository import (
    UserIdentityRecord,
    UserRecord,
    UserRepository,
)
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
from .device_authenticator import user_disabled_error
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

    登录签发路径由 :class:`UserRepository` 开外层事务（见
    :meth:`DevicePairingService._authorize`），因此这里的 :meth:`transaction` 只服务
    刷新与撤销这类**不涉及用户注册**的写入。
    """

    def transaction(self) -> AbstractContextManager[None]: ...

    def find_device_id_by_public_key(self, user_id: str, fingerprint: str) -> str | None: ...

    def create_paired_device(self, record: PairedDeviceRecord) -> None: ...

    def reactivate_paired_device(self, device_id: str, display_name: str, now: datetime) -> None: ...

    def revoke_device_credentials(self, device_id: str, now: datetime) -> None: ...

    def create_refresh_credential(self, record: DeviceRefreshCredentialRecord) -> None: ...

    def create_access_token(self, record: DeviceAccessTokenRecord) -> None: ...

    def load_refresh_credential(self, token_hash: str) -> DeviceRefreshCredentialRecord | None: ...

    def load_paired_device_auth(self, device_id: str) -> PairedDeviceAuthRecord | None: ...

    def load_device_user(self, device_id: str) -> UserRecord | None: ...

    def load_device_identity(self, device_id: str) -> UserIdentityRecord | None: ...

    def list_device_ids_for_user(self, user_id: str) -> Sequence[str]: ...

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
    """OAuth 登录成功后 App 获得的 Lyra 凭据，不含任何第三方 token。

    ``user`` 与 ``identity`` 一并返回：登录响应必须给出与 ``GET /v1/me`` 同形的
    ``user`` 对象（P0 契约 §3.9），而它只能来自本次解析出的用户与身份。
    """

    device_id: str
    display_name: str
    access_token: str
    access_token_expires_at: str
    refresh_token: str
    refresh_token_expires_at: str
    user: UserRecord
    identity: UserIdentityRecord


@dataclass(frozen=True)
class DeviceTokenRefreshHttpResponse:
    """刷新成功后返回的新一对凭据。"""

    access_token: str
    access_token_expires_at: str
    refresh_token: str
    refresh_token_expires_at: str


class DevicePairingService:
    """OAuth 登录后的设备会话管理。

    保留既有 access/refresh 凭据格式；匿名配对码入口已删除。所有签发、刷新与
    撤销在同一单写队列中执行。
    """

    def __init__(
        self,
        repository: DeviceRepository,
        write_coordinator: WriteCoordinator,
        users: UserRepository,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """@param repository: 设备会话仓储。
        @param write_coordinator: 应用级单写队列。
        @param users: 用户目录仓储；登录时按已验证身份解析或自动注册用户。
        @param now: 可信时钟；缺省用系统时间。
        """
        self._repository = repository
        self._writes = write_coordinator
        self._users = users
        self._now = now or (lambda: datetime.now(UTC))

    async def authorize_user_device(
        self,
        *,
        identity: LoginIdentity,
        display_name: str,
        public_key_pem: str,
        on_new_user: Callable[[str], None] | None = None,
    ) -> DevicePairingHttpResponse:
        """为已验证的身份签发设备凭据；新身份在此自动注册。

        **同一用户 + 同一公钥指纹复用设备 ID**：用户重装 App 后用同一把密钥重新登录，
        仍对应同一台设备，而不是每次登录都新增一台。指纹匹配带用户范围，因此另一
        用户持有同一把公钥不会顶掉本用户的设备。

        重新配对时**先撤销该设备的既有凭据再签发**，避免旧的 refresh 凭据在
        新会话建立后仍然可用。

        @param identity: 已验证的来源身份。
        @param display_name: 用户可识别的设备名称。
        @param public_key_pem: 设备公钥 PEM。
        @param on_new_user: **注册门禁**。仅当本次调用**新建**了用户时，在事务内以新
            用户 ID 调用它；抛错即整体回滚（用户、身份、设备与凭据都不落库）。生产
            唯一入口（登录兑换）注入"核销邀请码"，因此新身份没有可用码就注册不成；
            测试夹具直接签发既有身份/预置用户时不注入它，这是刻意的：门禁属于
            "注册"这条业务路径，不属于"给已有用户签发设备"这个动作。
        @returns 设备 ID、名称、一对新凭据，以及本次解析出的用户与身份。
        @raises ApplicationError: 公钥无效（400）或该身份的用户已停用（403）。
        """
        public_key = normalize_device_public_key(public_key_pem)
        return await self._writes.enqueue(lambda: self._authorize(
            identity, display_name, public_key, on_new_user
        ))

    async def _authorize(
        self,
        identity: LoginIdentity,
        display_name: str,
        public_key: Any,
        on_new_user: Callable[[str], None] | None,
    ) -> DevicePairingHttpResponse:
        """在单写队列内完成一次签发。

        **外层事务由用户仓储开启**，用户、身份、设备与凭据因此在同一个事务里提交，
        不会留下「有设备、没有身份」或「有用户、没有设备」的半成品。

        事务所有者不能反过来：DuckDB 在事务内再执行 ``BEGIN`` 会直接报错**并中止
        外层事务**，而 :class:`UserRepository` 的事务按连接可重入、设备仓储的不可
        重入。所以必须由用户仓储开外层，设备仓储的 ``transaction()`` 在这条路径上
        不被调用（刷新与撤销路径仍然用它）。

        **本方法体内不得出现 ``await``**：写协调器要求入队操作是可等待对象，但数据库
        事务一旦跨越挂起点，就会在持锁期间让出事件循环——另一个任务的写入可能因此
        插进这个事务，或与之互相等待。整段写入必须一口气跑完。
        """
        now = self._now()
        with self._users.transaction():
            # 自动注册发生在这里，也就是**完整 OAuth 验证与客户端 verifier 校验都已
            # 通过之后**；回调未完成、授权取消或 verifier 不对时根本走不到这一步。
            resolved = self._users.find_or_create_user_for_identity(identity, now=now)
            if resolved.created and on_new_user is not None:
                # **注册门禁在事务内执行**：核销失败会连带回滚刚建好的用户与身份，
                # 因此不存在"没凭邀请码却拿到了账号"的中间态。
                on_new_user(resolved.user.id)
            if resolved.user.status != "active":
                # 已停用的身份**保留原映射**、拒绝登录，而不是新建一个用户绕开停用。
                raise user_disabled_error()

            existing_id = self._repository.find_device_id_by_public_key(
                resolved.user.id, public_key.fingerprint
            )
            device_id = existing_id if existing_id is not None else str(uuid.uuid4())
            record = create_paired_device_record(
                device_id,
                display_name,
                public_key,
                now,
                user_id=resolved.user.id,
                identity_id=resolved.identity.id,
            )
            if existing_id is None:
                self._repository.create_paired_device(record)
                device_identity = resolved.identity
            else:
                self._repository.revoke_device_credentials(device_id, now)
                # 设备行已绑定身份且**不可改**（DuckDB 不允许更新被引用的父行外键列），
                # 因此这里重新读回它实际绑定的身份，让登录响应与 ``GET /v1/me`` 报出
                # 同一个身份——否则同一次登录会在两个接口上给出不同的 provider。
                self._repository.reactivate_paired_device(device_id, record.display_name, now)
                bound = self._repository.load_device_identity(device_id)
                if bound is None:
                    raise ApplicationError(
                        code="DEVICE_IDENTITY_MISSING",
                        message="设备身份关联缺失",
                        status_code=500,
                        retryable=False,
                    )
                device_identity = bound

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
            user=resolved.user,
            identity=device_identity,
        )

    async def refresh_tokens(self, *, refresh_token: str) -> DeviceTokenRefreshHttpResponse:
        """校验并轮换刷新凭据，同时签发新的访问令牌。

        **重用检测会撤销整个凭据族**：旧令牌再次出现说明它已经泄漏，
        只拒绝这一次请求等于把窗口留给攻击者。

        @param refresh_token: App 提交的明文刷新凭据。
        @returns 新的一对凭据。
        @raises ApplicationError: 凭据无效、已撤销、已重用或已过期；用户已停用
            返回 ``USER_DISABLED``（403）且**不轮换凭据**。
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
        # 用户状态先于轮换：停用用户不得用刷新把会话续下去。放在轮换之前还保证了
        # **不消耗**这枚刷新凭据——否则一次 403 会让客户端的凭据作废，恢复用户后
        # 还得重新走 OAuth。
        user = self._repository.load_device_user(record.device_id)
        if user is None:
            raise self._credential_invalid()
        if user.status != "active":
            raise user_disabled_error()
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

    async def revoke_user_devices(self, user_id: str) -> tuple[str, ...]:
        """撤销该用户的**全部**设备及其凭据，返回被撤销的设备 ID。

        停用用户时必须调用它：只把 ``users.status`` 改成 ``disabled`` 并不会让已经
        签发的访问令牌失效，那些令牌在过期前仍能通过认证（认证虽然会因停用拒绝，
        但恢复用户后旧令牌又会复活）。因此停用与撤销凭据要一起做——计划 §4.4
        「撤销该用户全部访问与刷新凭据」。

        返回设备 ID 让调用方能继续做级联（关闭事件流、终止在飞回合），而不是
        仅仅"报告停用完成"。

        @param user_id: 目标用户 UUID 文本。
        @returns 本次被撤销的设备 ID 元组（含此前已撤销的设备，便于级联收口）。
        """
        async def operation() -> tuple[str, ...]:
            now = self._now()
            device_ids = tuple(self._repository.list_device_ids_for_user(user_id))
            with self._repository.transaction():
                for device_id in device_ids:
                    self._repository.revoke_device_credentials(device_id, now)
                    self._repository.mark_device_revoked(device_id, now)
            return device_ids

        return await self._writes.enqueue(operation)

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

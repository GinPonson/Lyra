"""设备会话的 DuckDB 仓储。

把 :class:`DeviceRepository` 协议落到 schema 16 的真实表上：``paired_devices``、
``device_refresh_credentials``、``device_access_tokens``。

设备**归属用户**：``paired_devices`` 带 ``user_id`` 与 ``identity_id``，唯一键是
``(user_id, public_key_fingerprint)``，且 ``(user_id, identity_id)`` 有指向
``user_identities`` 的复合外键。因此本模块不再写 owner 身份表——那张表在迁移 16 后
已不存在，用户与身份的登记统一走 :class:`UserRepository`（P0 契约 §3.1/§3.7）。

**时间戳一律以 ISO UTC 文本进出**（毫秒 + ``Z``），与现有实现同形：领域层按文本
比较时间，若这里返回 ``datetime`` 对象，比较会静默失败。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import duckdb

from ...application.app_error import ApplicationError
from ...application.device_pairing_service import DeviceRepository
from ...security.device_auth import (
    DeviceAccessTokenRecord,
    DeviceRefreshCredentialRecord,
    PairedDeviceAuthRecord,
    PairedDeviceRecord,
    RotatedDeviceRefreshCredential,
)
from .user_repository import UserIdentityRecord, UserRecord, UserRepository


class DuckDbDeviceRepository(DeviceRepository):
    """基于 DuckDB 连接实现的设备会话仓储。

    @param connection: 已迁移到当前 schema 的连接；写操作由调用方的单写队列串行化。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection
        # 用户目录读取复用 P1a 的仓储，而不是在这里重写一份 users/user_identities SQL：
        # 状态与角色的解析规则只应有一处，否则两处会各自漂移。
        self._users = UserRepository(connection)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """在一个事务内执行后续写入；失败即回滚。

        @raises Exception: 原样抛出业务异常；若回滚本身也失败，则抛出聚合信息。
        """
        self._connection.execute("BEGIN TRANSACTION")
        try:
            yield
        except Exception as error:
            try:
                self._connection.execute("ROLLBACK")
            except Exception as rollback_error:  # noqa: BLE001
                raise RuntimeError(
                    f"设备凭据事务失败且回滚失败: {error!r} / 回滚错误: {rollback_error!r}"
                ) from error
            raise
        self._connection.execute("COMMIT")

    def find_device_id_by_public_key(self, user_id: str, fingerprint: str) -> str | None:
        """在**该用户范围内**按公钥指纹查找既有设备。

        范围必须带用户：唯一键已从全局指纹改为 ``(user_id, 指纹)``，不带范围会让
        两个持有同一把公钥的用户互相顶掉对方的设备行。

        @param user_id: 归属用户 UUID 文本。
        @param fingerprint: 设备公钥的 SHA-256 指纹。
        @returns 既有设备 ID；该用户没有这台设备时 ``None``。
        """
        row = self._connection.execute(
            """
            SELECT id FROM paired_devices
            WHERE user_id = CAST(? AS UUID) AND public_key_fingerprint = ?
            """,
            [user_id, fingerprint],
        ).fetchone()
        return None if row is None else str(row[0])

    def create_paired_device(self, record: PairedDeviceRecord) -> None:
        """插入一条新的活动设备记录（含归属用户与本次登录身份）。"""
        self._connection.execute(
            """
            INSERT INTO paired_devices
                (id, user_id, identity_id, display_name, public_key_pem,
                 public_key_fingerprint, key_algorithm,
                 status, paired_at, last_seen_at, revoked_at, updated_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, ?,
                    ?, CAST(? AS TIMESTAMPTZ), NULL, NULL, CAST(? AS TIMESTAMPTZ))
            """,
            [
                record.id, record.user_id, record.identity_id, record.display_name,
                record.public_key_pem, record.public_key_fingerprint, record.key_algorithm,
                record.status, record.paired_at, record.updated_at,
            ],
        )

    def reactivate_paired_device(self, device_id: str, display_name: str, now: datetime) -> None:
        """重新配对：恢复 active、清空撤销时间，且不回退 ``updated_at``。

        **不更新 ``user_id``，也不更新 ``identity_id``**：设备一旦归属用户就不再改
        owner，身份同理（契约 §3.1/§3.7）。这也不只是设计选择——DuckDB 1.5.6 对
        「被外键引用的父行」更新**外键列**会直接抛
        ``ConstraintException: ... is still referenced by a foreign key``（已实测），
        而该设备的凭据行必须保留（重放检测依赖它们），所以这条路径上不可能改身份。
        复合外键保证留存的身份始终与该设备同属一个用户。
        """
        self._connection.execute(
            """
            UPDATE paired_devices
            SET display_name = ?, status = 'active', revoked_at = NULL,
                updated_at = greatest(updated_at, CAST(? AS TIMESTAMPTZ))
            WHERE id = CAST(? AS UUID)
            """,
            [display_name, _iso(now), device_id],
        )

    def revoke_device_credentials(self, device_id: str, now: datetime) -> None:
        """撤销该设备仍活动的刷新凭据与访问令牌；已撤销的保持原时间戳。"""
        stamp = _iso(now)
        for table in ("device_refresh_credentials", "device_access_tokens"):
            self._connection.execute(
                f"""
                UPDATE {table}
                SET status = 'revoked', revoked_at = CAST(? AS TIMESTAMPTZ)
                WHERE device_id = CAST(? AS UUID) AND status = 'active'
                """,
                [stamp, device_id],
            )

    def create_refresh_credential(self, record: DeviceRefreshCredentialRecord) -> None:
        """插入一条刷新凭据记录（只含哈希）。"""
        self._connection.execute(
            """
            INSERT INTO device_refresh_credentials
                (id, device_id, family_id, generation, token_hash, status, expires_at,
                 last_used_at, rotated_at, revoked_at, created_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?, 
                    CAST(? AS TIMESTAMPTZ), NULL, NULL, NULL, CAST(? AS TIMESTAMPTZ))
            """,
            [
                record.id, record.device_id, record.family_id, record.generation,
                record.token_hash, record.status, record.expires_at, record.created_at,
            ],
        )

    def create_access_token(self, record: DeviceAccessTokenRecord) -> None:
        """插入一条访问令牌记录（只含哈希）。"""
        self._connection.execute(
            """
            INSERT INTO device_access_tokens
                (id, device_id, token_hash, status, expires_at, last_seen_at, revoked_at, issued_at)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), ?, ?, CAST(? AS TIMESTAMPTZ), NULL, NULL,
                    CAST(? AS TIMESTAMPTZ))
            """,
            [record.id, record.device_id, record.token_hash, record.status,
             record.expires_at, record.issued_at],
        )

    def load_refresh_credential(self, token_hash: str) -> DeviceRefreshCredentialRecord | None:
        """按令牌哈希读取刷新凭据。"""
        row = self._connection.execute(
            """
            SELECT id, device_id, family_id, generation, token_hash, status, expires_at,
                   last_used_at, rotated_at, revoked_at, created_at
            FROM device_refresh_credentials WHERE token_hash = ?
            """,
            [token_hash],
        ).fetchone()
        if row is None:
            return None
        return DeviceRefreshCredentialRecord(
            id=str(row[0]), device_id=str(row[1]), family_id=str(row[2]), generation=int(row[3]),
            token_hash=row[4], status=row[5], expires_at=_iso(row[6]),
            last_used_at=_iso(row[7]) if row[7] is not None else None,
            rotated_at=_iso(row[8]) if row[8] is not None else None,
            revoked_at=_iso(row[9]) if row[9] is not None else None,
            created_at=_iso(row[10]),
        )

    def load_access_token(self, token_hash: str) -> DeviceAccessTokenRecord | None:
        """按令牌哈希读取访问令牌记录。"""
        row = self._connection.execute(
            """
            SELECT id, device_id, token_hash, status, expires_at, last_seen_at, revoked_at, issued_at
            FROM device_access_tokens WHERE token_hash = ?
            """,
            [token_hash],
        ).fetchone()
        if row is None:
            return None
        return DeviceAccessTokenRecord(
            id=str(row[0]), device_id=str(row[1]), token_hash=row[2], status=row[3],
            expires_at=_iso(row[4]),
            last_seen_at=_iso(row[5]) if row[5] is not None else None,
            revoked_at=_iso(row[6]) if row[6] is not None else None,
            issued_at=_iso(row[7]),
        )

    def touch_access_token(self, token_id: str, now: datetime) -> None:
        """记录访问令牌的最近使用时间（审计用途）。"""
        self._connection.execute(
            """
            UPDATE device_access_tokens
            SET last_seen_at = CAST(? AS TIMESTAMPTZ)
            WHERE id = CAST(? AS UUID)
            """,
            [_iso(now), token_id],
        )

    def load_paired_device_auth(self, device_id: str) -> PairedDeviceAuthRecord | None:
        """读取设备的鉴权状态子集。"""
        row = self._connection.execute(
            "SELECT id, status, revoked_at FROM paired_devices WHERE id = CAST(? AS UUID)",
            [device_id],
        ).fetchone()
        if row is None:
            return None
        return PairedDeviceAuthRecord(
            id=str(row[0]),
            status=row[1],
            revoked_at=_iso(row[2]) if row[2] is not None else None,
        )

    def load_device_user(self, device_id: str) -> UserRecord | None:
        """读取设备的归属用户（认证与刷新时校验状态与角色）。

        @param device_id: 设备 UUID 文本。
        @returns 归属用户记录；设备不存在时 ``None``。
        """
        row = self._connection.execute(
            "SELECT user_id FROM paired_devices WHERE id = CAST(? AS UUID)", [device_id]
        ).fetchone()
        if row is None:
            return None
        return self._users.find_user_by_id(str(row[0]))

    def load_device_identity(self, device_id: str) -> UserIdentityRecord | None:
        """读取设备**本次登录**对应的已验证身份。

        ``GET /v1/me`` 的身份只能来自这条关联，不能由请求参数指定（契约 §3.9）；
        复合外键 ``(user_id, identity_id)`` 保证它与设备同属一个用户。

        @param device_id: 设备 UUID 文本。
        @returns 身份记录；设备或身份不存在时 ``None``。
        """
        row = self._connection.execute(
            """
            SELECT i.id, i.user_id, i.provider, i.subject, i.email, i.display_name,
                   i.created_at, i.updated_at
            FROM paired_devices AS d
            JOIN user_identities AS i
              ON i.id = d.identity_id AND i.user_id = d.user_id
            WHERE d.id = CAST(? AS UUID)
            """,
            [device_id],
        ).fetchone()
        if row is None:
            return None
        return UserIdentityRecord(
            id=str(row[0]),
            user_id=str(row[1]),
            provider=str(row[2]),  # type: ignore[arg-type] - 取值受 CHECK 约束限定为已知来源
            subject=str(row[3]),
            email=None if row[4] is None else str(row[4]),
            display_name=None if row[5] is None else str(row[5]),
            created_at=_iso(row[6]),
            updated_at=_iso(row[7]),
        )

    def list_device_ids_for_user(self, user_id: str) -> Sequence[str]:
        """列出该用户的全部设备 ID（含已撤销），供停用级联收口。

        含已撤销设备是刻意的：级联要关闭事件流、终止在飞回合，而这些资源可能还挂在
        一台刚被撤销的设备上。

        @param user_id: 用户 UUID 文本。
        @returns 设备 ID 元组，按配对时间排序。
        """
        rows = self._connection.execute(
            """
            SELECT id FROM paired_devices
            WHERE user_id = CAST(? AS UUID)
            ORDER BY paired_at, id
            """,
            [user_id],
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def load_refresh_credential_family(
        self, device_id: str, family_id: str
    ) -> Sequence[DeviceRefreshCredentialRecord]:
        """读取同设备同族的全部代次（含已轮换与已撤销）。"""
        rows = self._connection.execute(
            """
            SELECT id, device_id, family_id, generation, token_hash, status, expires_at,
                   last_used_at, rotated_at, revoked_at, created_at
            FROM device_refresh_credentials
            WHERE device_id = CAST(? AS UUID) AND family_id = CAST(? AS UUID)
            ORDER BY generation
            """,
            [device_id, family_id],
        ).fetchall()
        return tuple(
            DeviceRefreshCredentialRecord(
                id=str(row[0]), device_id=str(row[1]), family_id=str(row[2]),
                generation=int(row[3]), token_hash=row[4], status=row[5],
                expires_at=_iso(row[6]),
                last_used_at=_iso(row[7]) if row[7] is not None else None,
                rotated_at=_iso(row[8]) if row[8] is not None else None,
                revoked_at=_iso(row[9]) if row[9] is not None else None,
                created_at=_iso(row[10]),
            )
            for row in rows
        )

    def rotate_refresh_credential(
        self,
        rotation: RotatedDeviceRefreshCredential,
        access_record: DeviceAccessTokenRecord,
    ) -> None:
        """在同一事务中把旧代标记为 rotated、插入新代并签发访问令牌。

        **UPDATE 必须带状态与代号条件（CAS）**：单写队列只串行化**本进程**的写入，
        挡不住重放的旧令牌、重新配对的写入或另一个进程。无条件 UPDATE 会让一枚
        已被轮换/撤销的旧凭据静默换出新的 active 凭据，重用检测随后永远无法触发
        ——泄漏的整族凭据也就不会被撤销。

        @param rotation: 轮换计划：旧代的新状态与新代记录。
        @param access_record: 与本次轮换同时签发的访问令牌记录。
        @raises ApplicationError: CAS 失配（旧代已非 active，或代号已推进）。
        """
        # DuckDB 把 UPDATE 的受影响行数作为语句结果返回（一行一列）。
        changed = self._connection.execute(
            """
            UPDATE device_refresh_credentials
            SET status = ?, last_used_at = CAST(? AS TIMESTAMPTZ),
                rotated_at = CAST(? AS TIMESTAMPTZ)
            WHERE id = CAST(? AS UUID)
              AND status = 'active'
              AND generation = ?
            """,
            [rotation.previous.status, rotation.previous.last_used_at,
             rotation.previous.rotated_at, rotation.previous.id,
             rotation.previous.generation],
        ).fetchone()
        if changed is None or int(changed[0]) != 1:
            raise ApplicationError(
                code="DEVICE_CREDENTIAL_REUSED",
                message="设备刷新凭据已使用或撤销",
                status_code=401,
                retryable=False,
            )
        self.create_refresh_credential(rotation.next)
        self.create_access_token(access_record)

    def apply_family_revocation(self, plan: Any) -> None:
        """批量撤销一族凭据；已撤销的行保持原时间戳不变。"""
        for credential in plan.credentials:
            if credential.status != "revoked":
                continue
            self._connection.execute(
                """
                UPDATE device_refresh_credentials
                SET status = 'revoked', revoked_at = CAST(? AS TIMESTAMPTZ)
                WHERE id = CAST(? AS UUID) AND status <> 'revoked'
                """,
                [credential.revoked_at, credential.id],
            )

    def mark_device_revoked(self, device_id: str, now: datetime) -> None:
        """撤销设备；重复调用保留首次撤销时间。"""
        self._connection.execute(
            """
            UPDATE paired_devices
            SET status = 'revoked',
                revoked_at = coalesce(revoked_at, CAST(? AS TIMESTAMPTZ)),
                updated_at = greatest(updated_at, CAST(? AS TIMESTAMPTZ))
            WHERE id = CAST(? AS UUID)
            """,
            [_iso(now), _iso(now), device_id],
        )


def _iso(value: Any) -> str:
    """把 DuckDB 返回的时间值渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)

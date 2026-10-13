"""设备会话的 DuckDB 仓储。

把 :class:`DeviceRepository` 协议落到 schema 15 的真实表上：``paired_devices``、
``device_refresh_credentials``、``device_access_tokens``、``server_owner_identity``。

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
from ...infrastructure.auth.login_provider import LoginIdentity
from ...security.device_auth import (
    DeviceAccessTokenRecord,
    DeviceRefreshCredentialRecord,
    PairedDeviceAuthRecord,
    PairedDeviceRecord,
    RotatedDeviceRefreshCredential,
)


class DuckDbDeviceRepository(DeviceRepository):
    """基于 DuckDB 连接实现的设备会话仓储。

    @param connection: 已迁移到当前 schema 的连接；写操作由调用方的单写队列串行化。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

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

    def claim_server_owner_identity(self, identity: LoginIdentity, now: datetime) -> None:
        """登记该来源的 owner 身份。

        schema 15 的表是**每来源一行**（``provider`` 为主键），因此按 provider 更新；
        邮箱与昵称不参与跨来源关联。
        """
        stamp = _iso(now)
        self._connection.execute(
            """
            INSERT INTO server_owner_identity
                (singleton_key, provider, subject, email, display_name, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ))
            ON CONFLICT (provider) DO UPDATE SET
                subject = excluded.subject,
                email = excluded.email,
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            [identity.provider, identity.subject, identity.email, identity.display_name, stamp, stamp],
        )

    def find_device_id_by_public_key(self, fingerprint: str) -> str | None:
        """按公钥指纹查找既有设备；用于重装 App 后复用同一设备 ID。"""
        row = self._connection.execute(
            "SELECT id FROM paired_devices WHERE public_key_fingerprint = ?", [fingerprint]
        ).fetchone()
        return None if row is None else str(row[0])

    def create_paired_device(self, record: PairedDeviceRecord) -> None:
        """插入一条新的活动设备记录。"""
        self._connection.execute(
            """
            INSERT INTO paired_devices
                (id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
                 status, paired_at, last_seen_at, revoked_at, updated_at)
            VALUES (CAST(? AS UUID), ?, ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ), NULL, NULL,
                    CAST(? AS TIMESTAMPTZ))
            """,
            [
                record.id, record.display_name, record.public_key_pem,
                record.public_key_fingerprint, record.key_algorithm, record.status,
                record.paired_at, record.updated_at,
            ],
        )

    def reactivate_paired_device(self, device_id: str, display_name: str, now: datetime) -> None:
        """重新配对：恢复 active、清空撤销时间，并且不回退 ``updated_at``。"""
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

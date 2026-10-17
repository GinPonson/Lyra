"""邀请码仓储：单次有效、不过期、可撤销。

**只存 SHA-256 哈希**（与设备刷新凭据同一策略）。明文只在创建时返回一次，之后
任何读取路径都拿不到它——这决定了丢失只能重新签发，而不是"再查一次"。

三条实现约束：

1. **核销必须是条件更新**（``WHERE status = 'active'``）：否则两个并发注册会都读到
   ``active``、都核销成功、都建出用户——一枚码变两个人。CAS 让后到者拿到 0 行。
2. **核销与建用户在同一事务内**：先建用户再核销，核销失败就整体回滚；反过来先核销
   再建用户，建用户失败会白烧一枚码。
3. **列表永不返回哈希**：哈希本身足以在字典攻击成立时被反推，没有必要外泄。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import duckdb

from ...application.app_error import ApplicationError

# 邀请码明文长度：24 字节 → 32 个 base64url 字符。够长到不可穷举，又能口头/剪贴板传递。
_CODE_BYTES = 24
_CODE_PATTERN_LENGTH = 32

InviteCodeStatus = Literal["active", "used", "revoked"]


@dataclass(frozen=True)
class InviteCodeRecord:
    """一枚邀请码的运维视图；**不含明文，也不含哈希**。"""

    id: str
    label: str | None
    status: InviteCodeStatus
    created_by: str | None
    created_at: str
    used_by: str | None
    used_at: str | None
    revoked_at: str | None


def generate_invite_code() -> str:
    """生成一枚邀请码明文。

    @returns 32 个 base64url 字符（无填充）。
    """
    return secrets.token_urlsafe(_CODE_BYTES)


def hash_invite_code(code: str) -> str:
    """计算邀请码的存储哈希。

    **与设备凭据同算法**（UTF-8 的 SHA-256 小写十六进制）：邀请码是高熵随机串，
    不需要慢哈希；加盐反而会让"按哈希查一行"必须全表扫描。

    @param code: 邀请码明文。
    @returns 64 位十六进制。
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class InviteCodeRepository:
    """``invite_codes`` 的读写。

    @param connection: 已迁移到当前 schema 的连接。
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def create(
        self,
        *,
        created_by: str | None,
        label: str | None,
        now: datetime,
    ) -> tuple[InviteCodeRecord, str]:
        """签发一枚邀请码。

        @param created_by: 签发者用户 ID；``None`` 表示来自离线初始化入口。
        @param label: 备注（准备发给谁），可为空。
        @param now: 服务端时钟。
        @returns 记录与**唯一一次**返回的明文。
        """
        stamp = _iso(now)
        invite_id = str(uuid4())
        code = generate_invite_code()
        self._connection.execute(
            """
            INSERT INTO invite_codes (id, code_hash, label, status, created_by, created_at)
            VALUES (CAST(? AS UUID), ?, ?, 'active', ?, CAST(? AS TIMESTAMPTZ))
            """,
            [invite_id, hash_invite_code(code), label, created_by, stamp],
        )
        record = self.find_by_id(invite_id)
        if record is None:  # pragma: no cover - 刚插入就查不到说明连接异常
            raise RuntimeError("邀请码写入后无法读回")
        return record, code

    def find_by_id(self, invite_id: str) -> InviteCodeRecord | None:
        """按 ID 读取；不存在返回 ``None``。"""
        row = self._connection.execute(
            """
            SELECT id, label, status, created_by, created_at, used_by, used_at, revoked_at
            FROM invite_codes WHERE id = CAST(? AS UUID)
            """,
            [invite_id],
        ).fetchone()
        return None if row is None else _record(row)

    def find_by_code(self, code: str) -> InviteCodeRecord | None:
        """按明文查记录；用于 ``/start`` 的提前校验（**不核销**）。"""
        row = self._connection.execute(
            """
            SELECT id, label, status, created_by, created_at, used_by, used_at, revoked_at
            FROM invite_codes WHERE code_hash = ?
            """,
            [hash_invite_code(code)],
        ).fetchone()
        return None if row is None else _record(row)

    def is_active(self, code_hash: str) -> bool:
        """该哈希对应的码是否仍可用（存在、未用、未撤销）。"""
        row = self._connection.execute(
            "SELECT 1 FROM invite_codes WHERE code_hash = ? AND status = 'active'",
            [code_hash],
        ).fetchone()
        return row is not None

    def consume(self, *, code_hash: str, used_by: str, now: datetime) -> bool:
        """把一枚码核销给某个用户。

        **条件更新**：只有仍为 ``active`` 的行会被占用，后到的并发请求拿到 0 行。

        @param code_hash: 明文的哈希。
        @param used_by: 用掉它的用户 ID。
        @param now: 服务端时钟。
        @returns 是否核销成功。
        """
        row = self._connection.execute(
            """
            UPDATE invite_codes
            SET status = 'used', used_by = CAST(? AS UUID), used_at = CAST(? AS TIMESTAMPTZ)
            WHERE code_hash = ? AND status = 'active'
            RETURNING id
            """,
            [used_by, _iso(now), code_hash],
        ).fetchone()
        return row is not None

    def revoke(self, invite_id: str, *, now: datetime) -> InviteCodeRecord | None:
        """撤销一枚仍在活动状态的码。

        @returns 更新后的记录；码不存在或已不是 ``active`` 时返回 ``None``。
        """
        row = self._connection.execute(
            """
            UPDATE invite_codes
            SET status = 'revoked', revoked_at = CAST(? AS TIMESTAMPTZ)
            WHERE id = CAST(? AS UUID) AND status = 'active'
            RETURNING id
            """,
            [_iso(now), invite_id],
        ).fetchone()
        return None if row is None else self.find_by_id(invite_id)

    def list_codes(self, *, limit: int, offset: int) -> tuple[InviteCodeRecord, ...]:
        """按创建时间倒序分页读取。"""
        rows = self._connection.execute(
            """
            SELECT id, label, status, created_by, created_at, used_by, used_at, revoked_at
            FROM invite_codes
            ORDER BY created_at DESC, id
            LIMIT ? OFFSET ?
            """,
            [limit, offset],
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def count(self) -> int:
        """邀请码总数，供分页元数据使用。"""
        row = self._connection.execute("SELECT count(*) FROM invite_codes").fetchone()
        return int(row[0]) if row is not None else 0


def registration_blocked_error() -> ApplicationError:
    """新用户没有可用邀请码时的统一错误。

    ``/start`` 与 ``/exchange`` 共用：前者是"你还没填"，后者是"填了但已失效"。
    **不区分"不存在"与"已用/已撤销"**——区分会让持有任意一个码的人靠错误码枚举
    其他人的码状态。
    """
    return ApplicationError(
        code="INVITE_CODE_INVALID",
        message="邀请码无效或已被使用",
        status_code=403,
        retryable=False,
    )


def invite_required_error() -> ApplicationError:
    """新用户注册但未携带邀请码。"""
    return ApplicationError(
        code="INVITE_CODE_REQUIRED",
        message="首次注册需要邀请码",
        status_code=403,
        retryable=False,
    )


def _record(row: tuple[object, ...]) -> InviteCodeRecord:
    """把查询行映射为运维视图。"""
    return InviteCodeRecord(
        id=str(row[0]),
        label=None if row[1] is None else str(row[1]),
        status=row[2],  # type: ignore[arg-type]
        created_by=None if row[3] is None else str(row[3]),
        created_at=_iso(row[4]),
        used_by=None if row[5] is None else str(row[5]),
        used_at=None if row[6] is None else _iso(row[6]),
        revoked_at=None if row[7] is None else _iso(row[7]),
    )


def _iso(value: object) -> str:
    """把 DuckDB 返回的时间值渲染为与 JavaScript ``toISOString()`` 同形的文本。

    读回可能带本地偏移（如 ``+08:00``），写入用的是 ``Z``；不归一化会让同一行在
    写入与读回时给出不同字符串。
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return (
            moment.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)

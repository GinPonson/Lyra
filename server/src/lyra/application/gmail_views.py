"""Gmail 连接的公开视图。

镜像 TypeScript 版 ``gmail-connection-view.ts`` 与 ``gmail-account-mapping-view.ts``。

**公开视图刻意不暴露这些字段**：

| 字段 | 不暴露的原因 |
| --- | --- |
| ``encrypted_refresh_token`` | 密文虽不可直接使用，但泄露它等于给出离线解密的靶子 |
| ``google_subject`` | 内部标识，客户端只需要邮箱 |
| ``granted_scopes`` | 属于服务端配置细节 |
| ``history_id`` | 内部同步游标，客户端不该据此判断状态 |

**连接视图还会校验 ``last_error_code`` 是已知错误码。** 一行含未知错误码说明它被别的
写入路径污染了；**照原样投影会把任意文本送到客户端**。拒绝投影比泄漏安全。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.gmail.sync_plan import is_gmail_sync_error_code


@dataclass(frozen=True)
class PublicGmailConnection:
    """可安全返回给客户端的连接。"""

    id: str
    google_email: str
    status: str
    last_error_code: str | None
    last_sync_at: str | None
    created_at: str
    updated_at: str

    def to_json(self) -> dict[str, Any]:
        """投影为公开的 camelCase 字段。"""
        return {
            "id": self.id,
            "googleEmail": self.google_email,
            "status": self.status,
            "lastErrorCode": self.last_error_code,
            "lastSyncAt": self.last_sync_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class PublicGmailAccountMapping:
    """可安全返回给客户端的账户映射。"""

    id: str
    account_suffix: str
    account_id: str
    created_at: str
    updated_at: str

    def to_json(self) -> dict[str, Any]:
        """投影为公开的 camelCase 字段。"""
        return {
            "id": self.id,
            "accountSuffix": self.account_suffix,
            "accountId": self.account_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def create_public_gmail_connection(connection: Any) -> PublicGmailConnection:
    """投影一条连接。

    @param connection: 持久化的连接行。
    @returns 公开视图。
    @raises ValueError: ``last_error_code`` 不是已知的稳定错误码。
    """
    error_code = connection.last_error_code
    if error_code is not None and not is_gmail_sync_error_code(error_code):
        # 未知错误码说明该行被别的写入路径污染了；**照原样投影会把任意文本送到
        # 客户端**。拒绝投影比泄漏安全。
        raise ValueError("Gmail 连接包含非稳定错误码")
    return PublicGmailConnection(
        id=connection.id, google_email=connection.google_email,
        status=connection.status, last_error_code=error_code,
        last_sync_at=connection.last_sync_at,
        created_at=connection.created_at, updated_at=connection.updated_at,
    )


def create_public_gmail_account_mapping_list(
    connection_id: str, mappings: tuple[Any, ...]
) -> tuple[PublicGmailAccountMapping, ...]:
    """投影某连接的账户映射列表。

    **先校验每一条都属于该连接**：仓储若因过滤失效返回了别的连接的行，直接报错而不是
    把它们展示给用户——那会泄露另一个连接的账户归属。

    @param connection_id: 请求的连接 ID。
    @param mappings: 仓储返回的映射行。
    @returns 按尾号与 ID 排序的公开视图。
    @raises ValueError: 存在不属于该连接的行。
    """
    if any(mapping.connection_id != connection_id for mapping in mappings):
        raise ValueError("Gmail 账户映射不属于当前连接")
    ordered = sorted(
        mappings, key=lambda mapping: (mapping.account_suffix, mapping.id)
    )
    return tuple(
        PublicGmailAccountMapping(
            id=mapping.id, account_suffix=mapping.account_suffix,
            account_id=mapping.account_id, created_at=mapping.created_at,
            updated_at=mapping.updated_at,
        )
        for mapping in ordered
    )

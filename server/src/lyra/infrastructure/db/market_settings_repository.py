"""行情设置的持久化。

镜像 TypeScript 版 ``market-settings-repository.ts``。

**保存用 revision 做比较并交换**：行情设置是**每用户单例**行，同一用户的两台设备
同时打开设置页时，后保存的一方若直接覆盖，就会**静默丢掉**先保存的那份修改。CAS 让
后者收到 ``MARKET_SETTINGS_CHANGED`` 并重新读取。

**revision 按用户独立**：表的主键是 ``user_id``，行与行之间没有先后关系。A 保存到
revision 1 不影响 B 的 revision——只有一个"全局当前修订号"时，A 的保存会把 B 的
CAS 判定一并推进或拒绝（计划 §4.4：设置 revision 必须保持归属边界）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import duckdb

from ...application.app_error import ApplicationError
from ...domain.market_data.settings import MarketRoutes, normalize_routes


@dataclass(frozen=True)
class StoredMarketSettings:
    """某个用户持久化的行情设置。"""

    revision: int
    routes: MarketRoutes
    updated_at: str


def load_market_settings(
    connection: duckdb.DuckDBPyConnection, user_id: str
) -> StoredMarketSettings | None:
    """读取**该用户**的设置行；不存在返回 ``None``。

    ``user_id`` 是必填的位置参数，刻意放在资源之前：两个参数都是字符串，漏传不会
    报错，只会读到别人的设置行（P0 契约 §3.11 的同一理由）。

    不存在的语义是"从未保存过"，与"保存了空路由"不同——前者用 revision 0 表示。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @returns 该用户的设置；从未保存过时 ``None``。
    """
    row = connection.execute(
        """
        SELECT revision, routes_json, updated_at
        FROM market_source_settings
        WHERE user_id = CAST(? AS UUID)
        """,
        [user_id],
    ).fetchone()
    if row is None:
        return None
    routes = row[1]
    if isinstance(routes, str):
        routes = json.loads(routes)
    return StoredMarketSettings(
        revision=int(row[0]),
        routes=normalize_routes(routes),
        updated_at=_iso(row[2]),
    )


def persist_market_settings(
    connection: duckdb.DuckDBPyConnection,
    user_id: str,
    *,
    expected_revision: int,
    routes: MarketRoutes,
    now: datetime,
) -> int:
    """保存**该用户**的设置并推进其 revision。

    @param connection: 数据库连接。
    @param user_id: 当前用户的 UUID 文本。
    @param expected_revision: 调用方读到的 revision；CAS 的期望值。
    @param routes: 新路由。
    @param now: 服务端可信时间。
    @returns 新的 revision。
    @raises ApplicationError: 该用户的设置已被其它设备更新（409）。
    """
    stored = load_market_settings(connection, user_id)
    current = 0 if stored is None else stored.revision
    if current != expected_revision:
        raise ApplicationError(
            code="MARKET_SETTINGS_CHANGED",
            message="行情设置已在其他设备更新，请重新读取后再保存",
            status_code=409,
            retryable=False,
        )
    next_revision = current + 1
    payload = json.dumps(
        {asset_type: list(providers) for asset_type, providers in routes.items()},
        separators=(",", ":"), ensure_ascii=False,
    )
    connection.execute(
        """
        INSERT INTO market_source_settings (user_id, revision, routes_json, updated_at)
        VALUES (CAST(? AS UUID), ?, CAST(? AS JSON), CAST(? AS TIMESTAMPTZ))
        ON CONFLICT (user_id) DO UPDATE SET
            revision = excluded.revision,
            routes_json = excluded.routes_json,
            updated_at = excluded.updated_at
        """,
        [user_id, next_revision, payload, _iso(now)],
    )
    return next_revision


def _iso(value: object) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value)

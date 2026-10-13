"""行情设置的持久化。

镜像 TypeScript 版 ``market-settings-repository.ts``。

**保存用 revision 做比较并交换**：行情设置是单例行，两台设备同时打开设置页时，
后保存的一方若直接覆盖，就会**静默丢掉**先保存的那份修改。CAS 让后者收到
``MARKET_SETTINGS_CHANGED`` 并重新读取。
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
    """持久化的行情设置。"""

    revision: int
    routes: MarketRoutes
    updated_at: str


def load_market_settings(connection: duckdb.DuckDBPyConnection) -> StoredMarketSettings | None:
    """读取单例设置行；不存在返回 ``None``。

    不存在的语义是"从未保存过"，与"保存了空路由"不同——前者用 revision 0 表示。
    """
    row = connection.execute(
        "SELECT revision, routes_json, updated_at FROM market_source_settings"
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
    *,
    expected_revision: int,
    routes: MarketRoutes,
    now: datetime,
) -> int:
    """保存设置并推进 revision。

    @param connection: 数据库连接。
    @param expected_revision: 调用方读到的 revision。
    @param routes: 新路由。
    @param now: 服务端可信时间。
    @returns 新的 revision。
    @raises ApplicationError: 设置已被其它设备更新（409）。
    """
    stored = load_market_settings(connection)
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
        INSERT INTO market_source_settings (singleton_key, revision, routes_json, updated_at)
        VALUES (1, ?, CAST(? AS JSON), CAST(? AS TIMESTAMPTZ))
        ON CONFLICT (singleton_key) DO UPDATE SET
            revision = excluded.revision,
            routes_json = excluded.routes_json,
            updated_at = excluded.updated_at
        """,
        [next_revision, payload, _iso(now)],
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

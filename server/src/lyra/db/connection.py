"""DuckDB 连接、迁移与健康探测。

镜像 TypeScript 版 ``database.ts``。这里有一个**部署相关**的关键点：DuckDB
默认的 ``temp_directory`` 是相对当前工作目录的 ``.tmp``，而容器以只读根文件
系统运行、工作目录为 ``/app``，任何需要溢出的查询（大文件导入、分析）都会在
运行期失败。因此溢出目录固定到账本文件同级的 ``tmp/``，既满足只读根文件
系统，又让溢出文件随账本卷一起留存。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, TypedDict

import duckdb

from .migrations import apply_migrations
from .serialized_connection import SerializedDuckDBConnection


class DatabaseHealthAvailable(TypedDict):
    """数据库可用时的健康信息。"""

    status: Literal["available"]
    schemaVersion: int
    ledgerVersion: int


class DatabaseHealthUnavailable(TypedDict):
    """数据库不可用时的健康信息；不携带路径与底层错误。"""

    status: Literal["unavailable"]


DatabaseHealth = DatabaseHealthAvailable | DatabaseHealthUnavailable

_HEALTH_SQL = """
SELECT
  (SELECT max(version) FROM schema_migrations) AS schema_version,
  coalesce((SELECT max(version) FROM ledger_versions), 0) AS ledger_version
"""


class LyraDatabase:
    """单进程共用的 DuckDB 连接；仅由 :func:`open_lyra_database` 在迁移成功后创建。"""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        # 仓储会长期捕获 database.connection；包装必须在此处一次创建并一直复用，
        # 才能让所有请求共享同一把连接锁。
        self.connection = SerializedDuckDBConnection(connection)
        self._closed = False

    def read_health(self) -> DatabaseHealth:
        """读取健康端点需要的实际 schema 与账本版本。

        @returns 可用时返回两个版本号，连接已关闭时返回不可用。
        @throws RuntimeError: 健康查询没有返回任何行。
        """
        if self._closed:
            return {"status": "unavailable"}
        row = self.connection.execute(_HEALTH_SQL).fetchone()
        if row is None:
            raise RuntimeError("DuckDB 健康查询未返回结果")
        return {
            "status": "available",
            "schemaVersion": _require_version(row[0], "schema version"),
            "ledgerVersion": _require_version(row[1], "ledger version"),
        }

    def close(self) -> None:
        """关闭连接；重复调用保持幂等。"""
        if self._closed:
            return
        self.connection.close()
        self._closed = True


def _require_version(value: object, label: str) -> int:
    """把健康查询结果收窄为非负整数。

    @param value: 查询返回的原始值。
    @param label: 出错时用于定位的字段名。
    @returns 非负整数版本。
    @throws RuntimeError: 值不是非负整数。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{label} 必须是非负整数")
    return value


def _configure_temp_directory(connection: duckdb.DuckDBPyConnection, database_path: str) -> None:
    """把 DuckDB 的溢出目录固定到账本文件同级的 ``tmp/``。

    @param connection: 已打开的连接。
    @param database_path: 账本文件路径；``:memory:`` 时保留 DuckDB 默认行为。
    @throws ValueError: 路径含单引号——``SET`` 不接受参数绑定，必须直接拒绝。
    """
    if database_path.startswith(":memory:"):
        return
    directory = Path(database_path).resolve().parent / "tmp"
    if "'" in str(directory):
        raise ValueError("DuckDB 数据目录不能包含单引号")
    directory.mkdir(parents=True, exist_ok=True)
    connection.execute(f"SET temp_directory = '{directory}'")


def open_lyra_database(database_path: str) -> LyraDatabase:
    """打开 Lyra DuckDB，并在同一事务中执行和核验 v1–v15 迁移。

    @param database_path: DuckDB 文件路径；测试可传 ``:memory:``。
    @returns 已迁移、可供单进程服务使用的数据库。
    @throws ValueError: 路径为空。
    @throws Exception: 迁移失败；连接已在此前释放，不遗留句柄。
    """
    normalized_path = database_path.strip()
    if not normalized_path:
        raise ValueError("DuckDB 路径不能为空")

    connection: duckdb.DuckDBPyConnection | None = None
    try:
        connection = duckdb.connect(normalized_path)
        _configure_temp_directory(connection, normalized_path)
        apply_migrations(connection)
        return LyraDatabase(connection)
    except Exception:
        # 打开失败时不留下半开的连接：DuckDB 的文件锁会让后续进程完全无法打开，
        # 这一点已实测（见设计文档附录 A）。
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - 清理失败不应掩盖原始错误
                pass
        raise


def default_database_path() -> str:
    """返回配置中的账本路径；仅用于便利，不提供隐式默认值。

    @returns 环境变量 ``LYRA_DATABASE_PATH`` 的值。
    @throws KeyError: 未配置时抛出，避免服务在未知路径上静默创建空账本。
    """
    return os.environ["LYRA_DATABASE_PATH"]

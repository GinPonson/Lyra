"""账本冷备份 **并立即验证**。

**为什么把备份与验证做成一步**：运行手册自己写着"未验证的备份等于没有备份"。分成两步的
话，验证是那个在紧张时被跳过的步骤——而它的代价只有在真正需要回退时才会显现。

用法：

    cd server
    UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \\
      uv run --python 3.12 python scripts/backup_ledger.py \\
      --database "$LYRA_DATABASE_PATH" --output-dir /var/backups/lyra \\
      --i-have-stopped-the-server

**必须显式确认服务已停止**：带着活跃写入者复制 DuckDB 文件会得到一个**看起来正常但内部
不一致**的快照——而验证查询很可能仍然通过，因为表和行都在。

**备份不含 `application-gin.yml`**：它含敏感凭据，不得进入任何归档或 git 操作。脚本只处理
数据库文件，不触碰配置目录。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# 验证备份时读取的表。**选它们是因为涵盖了"账本主体"、"凭据"与"迁移状态"三类**：
# 只查一张表可能漏掉"主体可读但凭据表损坏"这类问题。
_VERIFY_QUERIES: tuple[tuple[str, str], ...] = (
    ("模式版本", "SELECT max(version) FROM schema_migrations"),
    ("账户数", "SELECT count(*) FROM accounts"),
    ("交易数", "SELECT count(*) FROM transactions"),
    ("账本版本数", "SELECT count(*) FROM ledger_versions"),
    ("配对设备数", "SELECT count(*) FROM paired_devices"),
)


def _sha256(path: Path) -> str:
    """计算文件摘要，便于日后确认备份未被改动。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(backup: Path) -> list[tuple[str, object]]:
    """以**只读**方式打开备份并跑验证查询。

    @param backup: 备份文件路径。
    @returns 每项的名称与取值。
    @raises Exception: 打不开或查询失败——**让失败抛出，不要吞掉**。
    """
    import duckdb

    connection = duckdb.connect(str(backup), read_only=True)
    try:
        return [
            (label, connection.execute(sql).fetchone()[0])
            for label, sql in _VERIFY_QUERIES
        ]
    finally:
        connection.close()


def main() -> int:
    """执行备份与验证。"""
    parser = argparse.ArgumentParser(description="账本冷备份并验证")
    parser.add_argument("--database", required=True, help="账本数据库路径")
    parser.add_argument("--output-dir", required=True, help="备份输出目录")
    parser.add_argument(
        "--i-have-stopped-the-server",
        action="store_true",
        help="确认写入者已停止（**不确认则拒绝执行**）",
    )
    args = parser.parse_args()

    if not args.i_have_stopped_the_server:
        print("拒绝执行：必须确认已停止服务（--i-have-stopped-the-server）。")
        print()
        print("带活跃写入者复制 DuckDB 会得到**看起来正常但内部不一致**的快照——")
        print("而验证查询很可能仍然通过，因为表和行都在。")
        return 2

    source = Path(args.database)
    if not source.exists():
        print(f"找不到数据库：{source}")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = output_dir / f"{source.name}.bak-{stamp}"

    print(f"复制 {source} → {backup}")
    shutil.copy2(source, backup)
    # DuckDB 的 WAL 若存在必须一并保留，否则备份可能缺少最近的事务。
    wal = source.with_name(source.name + ".wal")
    if wal.exists():
        shutil.copy2(wal, backup.with_name(backup.name + ".wal"))
        print(f"  含 WAL：{wal.name}")

    print("验证备份（只读打开 + 关键表查询）…")
    try:
        results = _verify(backup)
    except Exception as error:  # noqa: BLE001 - 这里要让失败可见并中止
        print(f"**验证失败：{error}**")
        print()
        print("备份文件已保留，但**它不可用**。不要开始切换。")
        return 1

    digest = _sha256(backup)
    print()
    print("=" * 68)
    print("备份验证记录（**请复制进运行手册文末**）")
    print("=" * 68)
    print(f"时间（UTC）      : {stamp}")
    print(f"源数据库         : {source}")
    print(f"备份文件         : {backup}")
    print(f"SHA-256          : {digest}")
    for label, value in results:
        print(f"{label:<16} : {value}")
    print("-" * 68)
    print("未包含 `application-gin.yml`——它含敏感凭据，不得进入任何归档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

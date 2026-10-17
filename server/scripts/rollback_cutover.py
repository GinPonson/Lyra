"""切流回退：用备份还原账本。

**回退是整个切换里唯一"一旦做错就同时失去两边"的操作**——因此这个脚本的核心不是
"能还原文件"，而是**三道拒绝**：

1. **不确认 Python 已停止就拒绝执行**——两个进程争同一个 DuckDB 文件锁只会都失败；
2. **备份自身验证不通过就拒绝覆盖**——一个损坏的备份会把当前库也毁掉，那时两份都没了；
3. **不把当前库另存就拒绝覆盖**——切换窗口内写入的数据虽然不在新库里，但**保留成文件
   总比直接丢掉好**（运行手册说"这些写入会丢失"，而它们其实不必不可逆地消失）。

用法：

    cd server
    UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \\
      uv run --python 3.12 python scripts/rollback_cutover.py \\
      --database "$LYRA_DATABASE_PATH" --backup /var/backups/lyra/ledger.duckdb.bak-... \\
      --i-have-stopped-python
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# 与备份脚本使用同一组验证查询：覆盖迁移状态、账本主体与凭据三类。
_VERIFY_QUERIES: tuple[tuple[str, str], ...] = (
    ("模式版本", "SELECT max(version) FROM schema_migrations"),
    ("账户数", "SELECT count(*) FROM accounts"),
    ("交易数", "SELECT count(*) FROM transactions"),
    ("账本版本数", "SELECT count(*) FROM ledger_versions"),
    ("配对设备数", "SELECT count(*) FROM paired_devices"),
)


def _verify(path: Path) -> list[tuple[str, object]]:
    """只读打开并跑验证查询。

    @param path: 数据库文件。
    @returns 每项的名称与取值。
    @raises Exception: 打不开或查询失败。
    """
    import duckdb

    connection = duckdb.connect(str(path), read_only=True)
    try:
        return [
            (label, connection.execute(sql).fetchone()[0])
            for label, sql in _VERIFY_QUERIES
        ]
    finally:
        connection.close()


def main() -> int:
    """执行回退。"""
    parser = argparse.ArgumentParser(description="用备份还原账本")
    parser.add_argument("--database", required=True, help="当前账本数据库路径")
    parser.add_argument("--backup", required=True, help="要还原的备份文件")
    parser.add_argument(
        "--i-have-stopped-python",
        action="store_true",
        help="确认 Python 服务已停止（**不确认则拒绝执行**）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证备份并打印将要执行的动作，不改动任何文件",
    )
    args = parser.parse_args()

    # 拒绝一：写入者未停止。
    if not args.i_have_stopped_python:
        print("拒绝执行：必须确认已停止 Python 服务（--i-have-stopped-python）。")
        print()
        print("DuckDB 是单进程文件锁：两个进程争同一个文件只会都失败，")
        print("而失败发生在**回退过程中**——那是最不该出意外的时刻。")
        return 2

    backup = Path(args.backup)
    if not backup.exists():
        print(f"找不到备份：{backup}")
        return 1

    # 拒绝二：**覆盖前验证备份**。
    print(f"验证备份 {backup} …")
    try:
        backup_state = _verify(backup)
    except Exception as error:  # noqa: BLE001 - 失败必须可见
        print(f"**备份不可用：{error}**")
        print()
        print("已中止——**没有改动任何文件**。")
        print("一个损坏的备份若被用来覆盖，当前库也会一并失去。")
        return 1
    for label, value in backup_state:
        print(f"  {label:<12}: {value}")

    target = Path(args.database)
    if not target.exists():
        print(f"当前数据库不存在：{target}（将以备份直接建立）")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    preserved = target.with_name(f"{target.name}.post-cutover-{stamp}")

    if args.dry_run:
        print()
        print("（--dry-run：以下动作未执行）")
        print(f"  1. 保存当前库 → {preserved}")
        print(f"  2. 用备份覆盖 → {target}")
        print("  3. 验证还原结果")
        return 0

    # 拒绝三：**先把当前库另存**，而不是直接覆盖。
    if target.exists():
        print(f"保存当前库 → {preserved}")
        shutil.copy2(target, preserved)
        # WAL 一并保留：切换窗口内的写入可能还在 WAL 里。
        wal = target.with_name(target.name + ".wal")
        if wal.exists():
            shutil.copy2(wal, preserved.with_name(preserved.name + ".wal"))

    print(f"还原 {backup} → {target}")
    # **整个文件还原，不做增量修补**——增量修补需要理解两侧的模式差异，而回退时刻
    # 没有做那种判断的余裕。
    shutil.copy2(backup, target)
    restored_wal = backup.with_name(backup.name + ".wal")
    live_wal = target.with_name(target.name + ".wal")
    if restored_wal.exists():
        shutil.copy2(restored_wal, live_wal)
    elif live_wal.exists():
        # 备份没有 WAL 而当前有——**删掉它**：留着会让旧的 WAL 重放到刚还原的库上。
        live_wal.unlink()
        print("  已移除当前 WAL（备份不含 WAL，留着会导致重放错位）")

    print("验证还原结果 …")
    try:
        restored = _verify(target)
    except Exception as error:  # noqa: BLE001
        print(f"**还原后的库无法打开：{error}**")
        print(f"当前库已保存于 {preserved}，可据此重新尝试。")
        return 1

    print()
    print("=" * 68)
    print("回退记录（**请复制进运行手册文末**）")
    print("=" * 68)
    print(f"时间（UTC）  : {stamp}")
    print(f"还原自       : {backup}")
    print(f"还原到       : {target}")
    print(f"切换期数据   : {preserved}（**未丢失，只是不在活动库里**）")
    for label, value in restored:
        print(f"{label:<12} : {value}")
    print("-" * 68)
    print("下一步：启动 TS 服务，并按阶段 3 重新对探针做一次确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

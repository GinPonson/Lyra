"""账本备份脚本的安全属性。

脚本的价值不在于"能复制文件"（`cp` 就能做），而在于它**拒绝在不安全的条件下工作**，
以及**把验证与备份绑成一步**。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "backup_ledger.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """跑备份脚本。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    """建一个空的账本数据库。"""
    from lyra.db.connection import open_lyra_database

    path = tmp_path / "ledger.duckdb"
    open_lyra_database(str(path)).close()
    return path


def test_refuses_without_explicit_confirmation(ledger: Path, tmp_path: Path) -> None:
    """**不确认服务已停止就拒绝执行。**

    带活跃写入者复制 DuckDB 会得到**看起来正常但内部不一致**的快照——而验证查询很可能
    仍然通过，因为表和行都在。因此这个确认不能是默认值。
    """
    result = _run("--database", str(ledger), "--output-dir", str(tmp_path / "out"))

    assert result.returncode != 0
    assert "拒绝执行" in result.stdout
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.bak-*"))


def test_backup_is_verified_and_recorded(ledger: Path, tmp_path: Path) -> None:
    """**备份被只读打开并查询关键表**，且输出可用于运行手册的记录。"""
    result = _run(
        "--database", str(ledger), "--output-dir", str(tmp_path / "out"),
        "--i-have-stopped-the-server",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SHA-256" in result.stdout
    assert "模式版本" in result.stdout
    # 验证查询真的跑出了值，而不是只打印了标签。
    assert ": 15" in result.stdout


def test_the_record_states_it_excludes_the_secret_config(ledger: Path, tmp_path: Path) -> None:
    """输出**明确声明不含 `application-gin.yml`**。

    该文件含敏感凭据，不得进入任何归档。让脚本自己声明这一点，比让人记得更可靠。
    """
    result = _run(
        "--database", str(ledger), "--output-dir", str(tmp_path / "out"),
        "--i-have-stopped-the-server",
    )

    assert "application-gin.yml" in result.stdout


def test_a_missing_database_is_reported(ledger: Path, tmp_path: Path) -> None:
    """源数据库不存在时明确报错。"""
    result = _run(
        "--database", str(tmp_path / "nope.duckdb"), "--output-dir", str(tmp_path / "out"),
        "--i-have-stopped-the-server",
    )

    assert result.returncode != 0
    assert "找不到数据库" in result.stdout


def test_the_backup_contains_the_same_schema_version(ledger: Path, tmp_path: Path) -> None:
    """**验证不是走过场**：备份里的模式版本与源库一致。"""
    result = _run(
        "--database", str(ledger), "--output-dir", str(tmp_path / "out"),
        "--i-have-stopped-the-server",
    )
    backups = sorted((tmp_path / "out").glob("*.bak-*"))

    assert result.returncode == 0
    assert len(backups) == 1

    import duckdb

    connection = duckdb.connect(str(backups[0]), read_only=True)
    try:
        version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    finally:
        connection.close()
    assert version == 15


# --------------------------------------------------------------------------
# 回退脚本
# --------------------------------------------------------------------------

ROLLBACK = Path(__file__).parent.parent / "scripts" / "rollback_cutover.py"


def _rollback(*args: str) -> subprocess.CompletedProcess[str]:
    """跑回退脚本。"""
    return subprocess.run(
        [sys.executable, str(ROLLBACK), *args],
        capture_output=True, text=True, check=False,
    )


def _backup(ledger: Path, out: Path) -> Path:
    """做一份已验证的备份并返回其路径。"""
    _run("--database", str(ledger), "--output-dir", str(out),
         "--i-have-stopped-the-server")
    return sorted(out.glob("*.bak-*"))[0]


def test_rollback_refuses_without_explicit_confirmation(ledger: Path, tmp_path: Path) -> None:
    """**不确认 Python 已停止就拒绝执行。**

    DuckDB 是单进程文件锁：两个进程争同一个文件只会都失败，**而失败发生在回退过程中**
    ——那是最不该出意外的时刻。
    """
    backup = _backup(ledger, tmp_path / "bk")
    result = _rollback("--database", str(ledger), "--backup", str(backup))

    assert result.returncode != 0
    assert "拒绝执行" in result.stdout


def test_a_corrupt_backup_does_not_destroy_the_current_database(
    ledger: Path, tmp_path: Path
) -> None:
    """**一个损坏的备份不得把当前库也毁掉。**

    这是回退脚本存在的主要理由。若先覆盖再验证，一次损坏的备份会让两份都没了——
    而"回退"这个动作本身已经意味着情况不好，不该再叠加一次不可逆的破坏。
    """
    corrupt = tmp_path / "corrupt.bak"
    corrupt.write_bytes(b"this is not a duckdb file")
    before = ledger.read_bytes()

    result = _rollback(
        "--database", str(ledger), "--backup", str(corrupt), "--i-have-stopped-python"
    )

    assert result.returncode != 0
    assert "没有改动任何文件" in result.stdout
    # **当前库逐字节未变。**
    assert ledger.read_bytes() == before


def test_the_cutover_window_data_is_preserved(ledger: Path, tmp_path: Path) -> None:
    """**切换窗口内的数据被保留成文件，而不是直接丢掉。**

    运行手册写着"切换后到回退前的所有写入都会丢失"——但**它们不必不可逆地消失**。
    保留一份文件几乎不花代价，而在回退后想捞回那段时间的写入时，它是唯一的东西。
    """
    backup = _backup(ledger, tmp_path / "bk")
    result = _rollback(
        "--database", str(ledger), "--backup", str(backup), "--i-have-stopped-python"
    )

    assert result.returncode == 0, result.stdout
    assert list(tmp_path.glob("ledger.duckdb.post-cutover-*")), "切换期数据未保留"
    assert "未丢失" in result.stdout


def test_dry_run_changes_nothing(ledger: Path, tmp_path: Path) -> None:
    """``--dry-run`` 只验证并打印，不改动任何文件。"""
    backup = _backup(ledger, tmp_path / "bk")
    before = ledger.read_bytes()

    result = _rollback(
        "--database", str(ledger), "--backup", str(backup),
        "--i-have-stopped-python", "--dry-run",
    )

    assert result.returncode == 0
    assert ledger.read_bytes() == before
    assert not list(tmp_path.glob("ledger.duckdb.post-cutover-*"))


def test_rollback_restores_a_working_database(ledger: Path, tmp_path: Path) -> None:
    """回退后**活动库可用**，且模式版本与备份一致。"""
    backup = _backup(ledger, tmp_path / "bk")
    result = _rollback(
        "--database", str(ledger), "--backup", str(backup), "--i-have-stopped-python"
    )

    assert result.returncode == 0

    import duckdb

    connection = duckdb.connect(str(ledger), read_only=True)
    try:
        version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    finally:
        connection.close()
    assert version == 15

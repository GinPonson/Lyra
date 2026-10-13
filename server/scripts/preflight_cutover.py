"""切换前预检。

**存在的理由**：运行手册的阶段 0 是一张人读的表格，而读表格的人会漏、会按记忆判断。
这个脚本把那些条件变成**可自动判定的断言**——通过就是通过，不通过就明确指出哪一条。

用法：

    cd server
    UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
      uv run --python 3.12 python scripts/preflight_cutover.py

退出码 0 表示全部通过；非 0 表示存在未满足的条件。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_PY = Path(__file__).resolve().parent.parent


class Check:
    """一条预检结果。"""

    def __init__(self, name: str, passed: bool, detail: str) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail


def _run(command: list[str], cwd: Path) -> tuple[int, str]:
    """执行命令并返回 (退出码, 输出末尾)。"""
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def check_full_test_suite() -> Check:
    """Python 全量测试必须全绿。"""
    code, output = _run(
        ["uv", "run", "--python", "3.12", "pytest", "-q"], SERVER_PY
    )
    tail = output.strip().splitlines()[-1] if output.strip() else "(无输出)"
    return Check("Python 全量测试", code == 0, tail)


def check_static_checks() -> Check:
    """错误类静态检查必须通过。"""
    code, output = _run(
        ["uv", "run", "--python", "3.12", "ruff", "check", "src/"], SERVER_PY
    )
    tail = output.strip().splitlines()[-1] if output.strip() else "(无输出)"
    return Check("错误类静态检查", code == 0, tail)


def check_can_take_over_an_existing_ledger() -> Check:
    """**能打开既有账本**——这是"接管"能力最直接的证据。

    夹具由迁移前的 TypeScript 服务端创建（schema 15、28 张表、无数据），因此这里验证的
    是"打开一个不是本实现造的库"，而不是"自己造自己读"。

    **它是冻结夹具而不是实时读取**：TypeScript 服务端已移除，实时读取会让这项检查随它
    一起消失——而"能不能接管既有库"恰恰是切换后**唯一不能重试**的能力。
    """
    fixture = (
        SERVER_PY / "tests" / "fixtures" / "typescript-ledger-schema15.duckdb"
    )
    if not fixture.is_file():
        return Check("接管既有账本", False, f"缺少夹具 {fixture.name}")
    try:
        code, output = _run(
            [
                "uv", "run", "--python", "3.12", "python", "-c",
                "import duckdb,sys;"
                f"c=duckdb.connect(r'{fixture}', read_only=True);"
                "v=c.execute('SELECT max(version) FROM schema_migrations').fetchone()[0];"
                "t=c.execute(\"SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='main'\").fetchone()[0];"
                "print(f'schema={v} tables={t}');c.close()",
            ],
            SERVER_PY,
        )
    except Exception as error:  # noqa: BLE001
        return Check("接管既有账本", False, f"检查自身失败: {error}")
    return Check("接管既有账本", code == 0, output.strip().splitlines()[-1] if output.strip() else "—")


def check_schema_version_is_self_consistent() -> Check:
    """**迁移清单的最后一条必须等于代码声明的模式版本。**

    这条检查此前是"Python 与 TypeScript 两侧常量一致"——那份耦合已随 TS 服务端移除。

    改为自身一致性：清单写到第 N 条，`CURRENT_SCHEMA_VERSION` 就必须是 N。**若两者分叉，
    代码会认为自己已迁移到 N，而实际磁盘上的库只到更早的版本**——症状是"服务启动正常，
    但某些表不存在"。
    """
    sys.path.insert(0, str(SERVER_PY / "src"))
    try:
        from lyra.db.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS
    except Exception as error:  # noqa: BLE001
        return Check("模式版本自洽", False, f"无法导入迁移清单: {error}")
    versions = [migration.version for migration in MIGRATIONS]
    passed = (
        versions == list(range(1, CURRENT_SCHEMA_VERSION + 1))
        and MIGRATIONS[-1].version == CURRENT_SCHEMA_VERSION
    )
    return Check(
        "模式版本自洽",
        passed,
        f"清单 {len(MIGRATIONS)} 条、末条 {MIGRATIONS[-1].version}、常量 {CURRENT_SCHEMA_VERSION}",
    )


def check_protocol_version() -> Check:
    """协议版本必须是 2——**它是 Flutter 客户端无需改动的依据**。"""
    text = (SERVER_PY / "src/lyra/protocol.py").read_text(encoding="utf-8")
    value = None
    for line in text.splitlines():
        if line.startswith("LYRA_PROTOCOL_VERSION"):
            value = line.split("=")[1].strip()
    return Check("协议版本为 2", value == "2", f"LYRA_PROTOCOL_VERSION={value}")


def check_route_contract() -> Check:
    """**实际路由必须与签入的契约一致。**

    契约在 ``tests/fixtures/route-manifest.json``：47 个 (方法, 路径) 对 + 1 个 WebSocket，
    即迁移期间与 TypeScript 版逐项对账的结果。

    此前这项检查直接读 TypeScript 的路由注册源码——那份耦合已随 TS 服务端移除。**保护
    没有消失，只是从"比对另一个实现"变成了"回归检测"。**

    **必须是 (方法, 路径) 对而不是路径集合**：只比路径会漏掉"某条路径上少了一个方法"，
    而这正是迁移期间真实发生过的误判（``POST /v1/ledger/accounts`` 整块缺失，路径级对账
    却把它算作一致）。
    """
    import json
    import re

    manifest_path = SERVER_PY / "tests" / "fixtures" / "route-manifest.json"
    if not manifest_path.is_file():
        return Check("路由契约", False, f"缺少 {manifest_path.name}")
    recorded = {
        (item["method"], item["path"])
        for item in json.loads(manifest_path.read_text(encoding="utf-8"))["pairs"]
        if item["method"] != "WS"
    }

    sys.path.insert(0, str(SERVER_PY / "src"))
    import tempfile

    from lyra.config import Settings
    from lyra.db.connection import open_lyra_database
    from lyra.main import create_app

    with tempfile.TemporaryDirectory() as work:
        database = open_lyra_database(str(Path(work) / "preflight.duckdb"))
        try:
            settings = Settings(
                LYRA_DATABASE_PATH=str(Path(work) / "preflight.duckdb"),
                LYRA_IMPORT_STAGING_DIR=str(Path(work) / "staging"),
                LYRA_SECRET_MASTER_KEY="0" * 64,
            )
            app = create_app(settings, database)
            served = {
                (method.upper(), re.sub(r"\{[^}]+\}", "{}", path))
                for path, ops in app.openapi()["paths"].items()
                if path.startswith("/v1")
                for method in ops
            }
        finally:
            database.close()

    missing = recorded - served
    extra = served - recorded
    passed = not missing and not extra
    detail = (
        f"{len(served)} 条与契约一致"
        if passed
        else f"缺失={sorted(missing)} 未记录={sorted(extra)}"
    )
    return Check("路由契约", passed, detail)


def check_encrypted_secret_master_key() -> Check:
    """``LYRA_SECRET_MASTER_KEY`` 必须是 64 位十六进制。

    切换后 **Python 要解开 TS 写入的真实密文**——密钥形态不对，Gmail 凭据会读不出来，
    而症状是"连接看起来存在但没有令牌"。
    """
    key = os.environ.get("LYRA_SECRET_MASTER_KEY", "")
    passed = len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key)
    return Check(
        "LYRA_SECRET_MASTER_KEY 形态",
        passed,
        "已设置且为 64 位十六进制" if passed else "未设置或形态不符（切换前必须设置）",
    )


def main() -> int:
    """跑完全部预检并打印结果。"""
    checks = [
        ("Python 全量测试", check_full_test_suite),
        ("错误类静态检查", check_static_checks),
        ("接管既有账本", check_can_take_over_an_existing_ledger),
        ("模式版本自洽", check_schema_version_is_self_consistent),
        ("协议版本", check_protocol_version),
        ("路由契约", check_route_contract),
        ("主密钥形态", check_encrypted_secret_master_key),
    ]
    results: list[Check] = []
    for name, run in checks:
        try:
            results.append(run())
        except Exception as error:  # noqa: BLE001 - 预检必须跑完所有项而非中断
            results.append(Check(name, False, f"检查自身失败: {error}"))

    print("=" * 68)
    print("切换前预检")
    print("=" * 68)
    for item in results:
        print(f"[{'通过' if item.passed else '未通过'}] {item.name}: {item.detail}")
    failed = [item for item in results if not item.passed]
    print("-" * 68)
    if failed:
        print(f"{len(failed)} 项未通过——**不要开始切换**。")
        return 1
    print("全部通过。")
    print()
    print("注意：本预检**不替代**运行手册的阶段 1（备份）与阶段 4（真实设备联调）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

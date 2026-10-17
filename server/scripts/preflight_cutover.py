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
from typing import Callable

SERVER_PY = Path(__file__).resolve().parent.parent
#: 由 TypeScript 侧产出的密文夹具（密钥是夹具自己的，见下方主密钥检查的说明）。
CRYPTO_FIXTURE = SERVER_PY / "tests" / "fixtures" / "crypto-fixtures.json"


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
    """错误类静态检查必须通过。

    **项目级扫描（`.`），不是只扫 `src/`**：规则配置在 `pyproject.toml`，只扫 `src/`
    会把 `scripts/` 与 `tests/` 里的错误类问题排除在信号之外——而"静态检查通过"这个
    信号一旦失真，它就不再是门禁，只是一句装饰。
    """
    code, output = _run(
        ["uv", "run", "--python", "3.12", "ruff", "check", "."], SERVER_PY
    )
    tail = output.strip().splitlines()[-1] if output.strip() else "(无输出)"
    return Check("错误类静态检查", code == 0, tail)


def check_can_take_over_an_existing_ledger() -> Check:
    """**能打开既有账本**——这是"接管"能力最直接的证据。

    夹具由迁移前的 TypeScript 服务端创建（schema 15、28 张表、无数据），因此这里验证的
    是"打开一个不是本实现造的库"，而不是"自己造自己读"。

    **它是冻结夹具而不是实时读取**：TypeScript 服务端已移除，实时读取会让这项检查随它
    一起消失——而"能不能接管既有库"恰恰是切换后**唯一不能重试**的能力。

    **在当前解释器里直接查，而不是 spawn 一个 `uv run`**：预检自己就跑在 `uv run` 里，
    `duckdb` 已经可用；再起一个 `uv run` 会在 `UV_CACHE_DIR` 不可写时失败，并被渲染成
    "接管既有账本未通过"——切换当天最不能误读的一个信号。
    """
    fixture = (
        SERVER_PY / "tests" / "fixtures" / "typescript-ledger-schema15.duckdb"
    )
    if not fixture.is_file():
        return Check("接管既有账本", False, f"缺少夹具 {fixture.name}")
    try:
        import duckdb

        connection = duckdb.connect(str(fixture), read_only=True)
        try:
            version = connection.execute(
                "SELECT max(version) FROM schema_migrations"
            ).fetchone()[0]
            tables = connection.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='main'"
            ).fetchone()[0]
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001 - 失败必须可见
        return Check("接管既有账本", False, f"无法只读打开夹具：{error}")
    return Check("接管既有账本", True, f"schema={version} tables={tables}")


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
    """协议版本必须是 3——**它是客户端与服务端同步升级后的契约版本**。"""
    text = (SERVER_PY / "src/lyra/protocol.py").read_text(encoding="utf-8")
    value = None
    for line in text.splitlines():
        if line.startswith("LYRA_PROTOCOL_VERSION"):
            value = line.split("=")[1].strip()
    return Check("协议版本为 3", value == "3", f"LYRA_PROTOCOL_VERSION={value}")


def check_route_contract() -> Check:
    """**实际路由必须与签入的契约一致。**

    契约在 ``tests/fixtures/route-manifest.json``：迁移期与 TypeScript 版逐项对账为
    47 个 (方法, 路径) 对 + 1 个 WebSocket（历史事实），多用户账本升级新增 3 项后当前
    合计 50 个 + 1 个 WebSocket。

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
    """``LYRA_SECRET_MASTER_KEY`` 必须存在、形态正确、并且真的能被密文路径用起来。

    三件事分开报：

    1. **形态**：64 位十六进制。形态不对时 `master_key_from_hex` 直接抛错，服务连启动
       都会失败——症状是"连接看起来存在但没有令牌"；
    2. **可用**：用这把密钥做一次加密/解密往返，确认它能走通密文路径；
    3. **是否与原部署一致**：拿冻结夹具（``tests/fixtures/crypto-fixtures.json``，由
       TypeScript 侧产出）里的密文试解一次——**只作为提示打印，不参与判定**。

    第 3 条为什么不参与判定：夹具用的是**夹具自己的密钥**，与任何真实部署的密钥无关。
    把"必须解开夹具密文"当成通过条件，会让这项检查在真实部署上**永远无法通过**——
    那正是本手册要删掉「TS 基线」那条检查的理由。要证明"这把密钥能解开你库里的密文"，
    只能用**生产库里的密文**验证，而预检刻意不打开生产库（DuckDB 单进程文件锁：TS 还在
    跑时连只读都打不开）。执行者在阶段 1 拿到冷备份后，可对备份文件离线做这项验证。
    """
    key = os.environ.get("LYRA_SECRET_MASTER_KEY", "")
    shaped = len(key) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in key
    )
    if not shaped:
        return Check(
            "LYRA_SECRET_MASTER_KEY 形态",
            False,
            "未设置或形态不符（切换前必须设置 64 位十六进制）",
        )

    sys.path.insert(0, str(SERVER_PY / "src"))
    try:
        from lyra.security.secret_cipher import SecretCipher, master_key_from_hex

        cipher = SecretCipher(master_key_from_hex(key))
        probe = "lyra-preflight-roundtrip"
        if cipher.decrypt(cipher.encrypt(probe, "preflight"), "preflight") != probe:
            return Check("LYRA_SECRET_MASTER_KEY 形态", False, "形态合法，但加解密往返不一致")
    except Exception as error:  # noqa: BLE001 - 失败必须可见
        return Check(
            "LYRA_SECRET_MASTER_KEY 形态", False, f"形态合法，但无法用于加密：{error}"
        )

    detail = "形态合法且可正常加解密"
    try:
        import json

        fixture = json.loads(CRYPTO_FIXTURE.read_text(encoding="utf-8"))
        first = fixture["cases"][0]
        cipher.decrypt(first["envelope"], first["context"])
    except Exception:  # noqa: BLE001 - 只作提示，不参与判定
        detail += (
            "；解不开冻结夹具里由 TS 产出的密文（夹具用的是它自己的密钥），"
            "因此**本条不能证明这把密钥就是原部署使用的那把**"
        )
    else:
        detail += "；并能解开冻结夹具里由 TS 产出的密文"
    return Check("LYRA_SECRET_MASTER_KEY 形态", True, detail)


#: 主流程的检查清单。
#:
#: **模块级常量，而不是 `main()` 里的局部列表**：`tests/test_preflight_script.py` 直接
#: 断言这份清单的内容。清单藏在函数体内时，删掉一项测试照样通过——"每一项都被接进
#: 主流程"就会从一条保证退化成一个愿望。
CHECKS: tuple[tuple[str, Callable[[], Check]], ...] = (
    ("Python 全量测试", check_full_test_suite),
    ("错误类静态检查", check_static_checks),
    ("接管既有账本", check_can_take_over_an_existing_ledger),
    ("模式版本自洽", check_schema_version_is_self_consistent),
    ("协议版本为 3", check_protocol_version),
    ("路由契约", check_route_contract),
    ("LYRA_SECRET_MASTER_KEY 形态", check_encrypted_secret_master_key),
)


def main() -> int:
    """跑完全部预检并打印结果。"""
    results: list[Check] = []
    for name, run in CHECKS:
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

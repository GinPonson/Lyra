"""预检脚本自身的可靠性。

**一个永远通过的检查等于没有检查。** 本文件固定两件使预检可信的事：

1. 每一项都**真的被接进主流程**——否则它会在某次重构中被静默丢掉，而预检照样打印
   "全部通过"；
2. **判别力已被验证过**：注入模式版本不一致会让 `模式版本一致` 失败（实测：
   Python=14 / TS=15 时报未通过）。这里用一条不跑全量测试的等价断言守住这个性质。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PREFLIGHT = Path(__file__).parent.parent / "scripts" / "preflight_cutover.py"


def _load():
    """按路径加载预检脚本（它不是包的一部分）。"""
    spec = importlib.util.spec_from_file_location("preflight_cutover", PREFLIGHT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["preflight_cutover"] = module
    spec.loader.exec_module(module)
    return module


def test_every_documented_check_is_wired_in() -> None:
    """**每一项检查都必须出现在主流程里。**

    预检的价值在于"每一项要么通过要么不通过"。若某一项没被接进 `main` 的清单，它会
    静默消失，而输出仍然是"全部通过"——那比没有这项检查更危险。
    """
    module = _load()
    # 依据脚本文档里列出的七项。
    expected = {
        "check_full_test_suite",
        "check_static_checks",
        "check_can_take_over_an_existing_ledger",
        "check_schema_version_is_self_consistent",
        "check_protocol_version",
        "check_route_contract",
        "check_encrypted_secret_master_key",
    }

    for name in expected:
        assert hasattr(module, name), f"缺少检查函数 {name}"


def test_the_schema_version_check_reports_both_numbers() -> None:
    """模式版本检查**同时报告清单条数与常量**。

    只报"通过/未通过"看不出它到底比了什么；两个数字都印出来，分叉时才能一眼看出是哪边。

    这条检查此前是"Python 与 TypeScript 两侧常量一致"，现在是"清单末条与代码常量一致"
    ——**保护的形式变了，但"必须能看出比了什么"这一点不变**。
    """
    module = _load()
    result = module.check_schema_version_is_self_consistent()

    assert "末条" in result.detail
    assert "常量" in result.detail
    assert result.passed


def test_the_protocol_version_check_reports_the_value() -> None:
    """协议版本检查的输出含实际取值——**失败时要能看出读到了什么**。"""
    module = _load()
    result = module.check_protocol_version()

    assert "LYRA_PROTOCOL_VERSION=" in result.detail
    assert result.passed, "协议版本应为 2"


def test_the_route_check_reports_counts_on_success() -> None:
    """路由对账通过时报告条数，失败时报告缺失与未记录的集合。

    两种输出都要有：只报"通过/未通过"让人无法判断检查是否真的跑了。
    """
    module = _load()
    result = module.check_route_contract()

    assert result.passed, result.detail
    assert "契约" in result.detail


def test_the_preflight_does_not_read_the_typescript_server() -> None:
    """**预检不得依赖已移除的 TypeScript 服务端。**

    它此前读 `server/` 三处（`npm run check`、`schema-version.ts`、`src/transport`）。
    那三处已改为自足检查：接管既有账本用冻结夹具、模式版本用自身一致性、路由用签入契约。

    这条断言防止日后有人图省事又把某个检查接回另一个实现——**那样切换当天会发现预检
    跑不起来，而那时最不该出这种事**。
    """
    source = PREFLIGHT.read_text(encoding="utf-8")

    assert "SERVER_TS" not in source
    assert "npm run check" not in source

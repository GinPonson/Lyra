"""财务工具目录测试。

目录的价值在于**与实际挂载的路由对账**。若只有一份静态清单，它会慢慢与实现漂移：
清单里有但路由没挂（Agent 调用得到 404），或路由挂了但清单没有（能力无法被发现）。

因此最要紧的一条是 :func:`test_catalog_matches_mounted_routes`。
"""

from __future__ import annotations

import pytest

from lyra.application.financial_tools import (
    FINANCIAL_TOOL_NAMES,
    FINANCIAL_TOOLS,
    UNIMPLEMENTED_TOOLS,
    is_financial_tool_name,
    tool_spec,
    tools_by_path,
    tools_in_category,
)


def test_catalog_has_eighteen_tools() -> None:
    """工具数量固定为 18；增减都需要同步提示词与客户端。"""
    assert len(FINANCIAL_TOOLS) == 18
    assert len(FINANCIAL_TOOL_NAMES) == 18


def test_tool_names_are_unique_and_ordered() -> None:
    """名字唯一，且顺序稳定（提示词与客户端都按顺序引用）。"""
    assert len(set(FINANCIAL_TOOL_NAMES)) == 18
    assert FINANCIAL_TOOL_NAMES[0] == "accounts"
    assert FINANCIAL_TOOL_NAMES[-1] == "market-positions"
    assert "summary" in FINANCIAL_TOOL_NAMES


def test_categories_split_as_expected() -> None:
    """分组数量与预期一致：账本 8、分析 9、行情 1。"""
    assert len(tools_in_category("ledger")) == 8
    assert len(tools_in_category("analytics")) == 9
    assert len(tools_in_category("market")) == 1


def test_paths_are_unique_and_prefixed_by_category() -> None:
    """路径唯一，且前缀与分组一致。"""
    mapping = tools_by_path()
    assert len(mapping) == 18
    for tool in FINANCIAL_TOOLS:
        assert tool.path.startswith(f"/v1/{tool.category}/"), tool.path


def test_unknown_tool_name_is_rejected() -> None:
    """未知工具名必须被拒，而不是当成"没有参数的工具"。"""
    assert is_financial_tool_name("summary") is True
    assert is_financial_tool_name("nonexistent") is False
    with pytest.raises(KeyError):
        tool_spec("nonexistent")


def test_unimplemented_set_is_a_subset_of_the_catalog() -> None:
    """未实现清单必须是目录的子集，否则说明名字写错。"""
    assert UNIMPLEMENTED_TOOLS <= set(FINANCIAL_TOOL_NAMES)


def test_catalog_matches_mounted_routes() -> None:
    """**目录与已挂载路由必须对账。**

    目录里有但路由没挂 → Agent 调用会得到 404；
    路由挂了但目录没有 → 能力无法被发现。
    两者都是静默失败，只有对账能拦住。
    """
    from fastapi.routing import APIRoute

    from lyra.api.import_routes import create_import_router
    from lyra.api.ledger_routes import create_ledger_router
    from lyra.api.market_routes import create_market_router

    # **必须覆盖目录里提到的每一类路由。** 这里原先只收集 ledger 与 import 两个
    # 路由器，于是 `market-positions` 的对账被静默跳过——它之所以没暴露，只是因为
    # 该工具当时还在未实现清单里、走的是另一个分支。
    # 对账测试的价值取决于它到底对了几条；漏掉一个路由器就等于漏掉一整类工具。
    mounted: set[str] = set()
    for router in (create_ledger_router(), create_import_router(), create_market_router()):
        for route in router.routes:
            if isinstance(route, APIRoute) and "GET" in route.methods:
                mounted.add(route.path)

    for tool in FINANCIAL_TOOLS:
        if tool.name in UNIMPLEMENTED_TOOLS:
            assert tool.path not in mounted, (
                f"{tool.name} 已实现但仍在 UNIMPLEMENTED_TOOLS 中"
            )
        else:
            assert tool.path in mounted, f"{tool.name} 声明已实现但路由未挂载"


def test_no_tool_remains_unimplemented() -> None:
    """**目录里的工具全部已实现。**

    这条断言写死内容是刻意的——**它刚刚履行了自己的职责**：`market-positions` 在
    M8 实现后，正是它失败并提醒把名字从未实现清单里移走。

    `UNIMPLEMENTED_TOOLS` 的存在意义是"实现补上后必须同时改这里，目录与实现不会
    悄悄分叉"；它现在是空的，说明 18 个工具全部可用。

    与 `test_catalog_matches_mounted_routes` 配合：前者说"没有欠账"，
    后者说"账目对得上"。
    """
    assert UNIMPLEMENTED_TOOLS == frozenset()
    assert len(FINANCIAL_TOOL_NAMES) == 18

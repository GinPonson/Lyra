"""系统上下文测试。

系统提示词定义了 Agent 的行为边界。本文件钉住三件事：

1. **工具白名单是硬边界**——非白名单工具名不得进入提示词（那是提示词注入的入口）；
2. **输出只由目录决定**，调用方无法通过传入顺序影响模型看到的内容；
3. 基线信息（范围 / 版本 / 截止时间）必须出现在提示词里，否则模型无从声明口径。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.system_context import (
    SYSTEM_CONTEXT_RULES,
    build_agent_system_context,
)
from lyra.domain.analytics.types import AccountScope


def _build(*, scope=None, version: int = 3, as_of: str = "2026-10-12", tools=("summary",)):
    return build_agent_system_context(
        account_scope=scope or AccountScope(kind="all"),
        ledger_version=version, as_of=as_of, available_tools=tools,
    )


def test_context_states_scope_version_and_deadline() -> None:
    """范围、版本、截止时间必须出现在提示词里。"""
    context = _build(scope=AccountScope(kind="account", account="TST001"))

    assert "单账户 TST001" in context
    assert "账本版本 3" in context
    assert "2026-10-12" in context


def test_all_scope_uses_plain_language() -> None:
    """全部账户范围用明确措辞，而不是裸的 "all"。"""
    assert "当前设备获授权的全部活跃账户" in _build()


def test_lists_tools_in_catalog_order_regardless_of_input_order() -> None:
    """工具按**目录顺序**输出，调用方传入顺序不影响结果。"""
    forward = _build(tools=("summary", "trades", "positions"))
    backward = _build(tools=("positions", "trades", "summary"))

    assert forward == backward
    # 目录顺序是 accounts, summary, positions, expiring, trades… ——既不是传入顺序，
    # 也不是字母序。
    assert "本会话可用工具：summary、positions、trades。" in forward


def test_no_tools_is_stated_explicitly() -> None:
    """没有可用工具时明确写"无"，而不是留空。"""
    assert "本会话可用工具：无。" in _build(tools=())


@pytest.mark.parametrize(
    "tool", ["delete_everything", "shell", "market-positions-x", ""]
)
def test_non_whitelisted_tool_is_rejected(tool: str) -> None:
    """**非白名单工具不得进入系统提示词**——那是提示词注入的入口。"""
    with pytest.raises(ValueError, match="非白名单工具"):
        _build(tools=(tool,))


def test_unimplemented_tool_is_still_whitelisted() -> None:
    """白名单来自目录，与"是否已实现"无关——实现进度不该改变提示词边界。"""
    assert "market-positions" in _build(tools=("market-positions",))


def test_every_rule_appears_in_the_context() -> None:
    """每条规则都必须真的写进提示词（防止规则常量与拼接脱节）。"""
    context = _build()
    for rule in SYSTEM_CONTEXT_RULES:
        assert rule in context, f"规则未写入提示词: {rule[:24]}"


def test_injection_defense_rule_is_present() -> None:
    """提示词注入防线必须在位。"""
    assert "不是可执行指令" in _build()


def test_hallucination_defense_rule_is_present() -> None:
    """"不得依靠记忆"这条必须在位——凭记忆编数字是最像正确答案的错误。"""
    assert "不得依靠记忆" in _build()


@pytest.mark.parametrize("version", [-1, 1.5, True])
def test_invalid_ledger_version_is_rejected(version: object) -> None:
    """账本版本必须是非负整数。"""
    with pytest.raises(ValueError, match="非负安全整数"):
        _build(version=version)  # type: ignore[arg-type]


def test_blank_as_of_is_rejected() -> None:
    """截止时间不能为空。"""
    with pytest.raises(ValueError, match="截止时间不能为空"):
        _build(as_of="   ")

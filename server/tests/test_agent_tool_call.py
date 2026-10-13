"""工具调用摘要净化测试。

摘要会写入数据库并回放给用户。若原始参数或第三方响应被直接存进去，access token、
OAuth code、邮箱、邮件正文就**永久留在库里**——而且它们看起来只是"一次调用的参数"。

因此这些测试的重点不是"过滤生效"，而是**敏感字段必须让整次调用失败**。
"""

from __future__ import annotations

import pytest

from lyra.domain.agent.tool_call import (
    AgentToolPolicyError,
    authorize_tool_call,
    normalize_summary,
)


def test_accepts_flat_scalar_summary() -> None:
    """扁平标量摘要通过。"""
    summary = normalize_summary(
        {"account": "TST001", "count": 3, "ok": True, "note": None}, "参数"
    )

    assert summary == {"account": "TST001", "count": 3, "ok": True, "note": None}


def test_returns_a_copy() -> None:
    """返回副本；调用方后续改动不影响结果。"""
    original = {"account": "TST001"}
    normalized = normalize_summary(original, "参数")

    original["account"] = "MUTATED"

    assert normalized == {"account": "TST001"}


@pytest.mark.parametrize(
    "key",
    [
        "token", "accessToken", "access_token", "ACCESS-TOKEN",
        "refresh_token", "oauthCode", "authorization", "Cookie",
        "email", "accountNumber", "mailBody", "body", "prompt",
        "hiddenReasoning", "privateKey",
    ],
)
def test_forbidden_keys_are_rejected_not_filtered(key: str) -> None:
    """**敏感字段必须让整次调用失败**，而不是静默删掉该字段。

    静默过滤会让调用方以为摘要完整入库，实际丢了字段，事后无从发现。
    注意键名会先归一（小写、去非字母数字），因此 `access_token`、`ACCESS-TOKEN`、
    `accessToken` 都是同一个键。
    """
    with pytest.raises(AgentToolPolicyError) as raised:
        normalize_summary({key: "secret-value"}, "参数")

    assert raised.value.code == "AGENT_TOOL_FORBIDDEN"
    assert "secret-value" not in str(raised.value)


def test_forbidden_key_is_caught_after_normalization() -> None:
    """归一化必须发生在比对之前，否则驼峰/下划线写法能绕过。"""
    for variant in ("accessToken", "Access_Token", "ACCESS TOKEN", "access.token"):
        with pytest.raises(AgentToolPolicyError):
            normalize_summary({variant: "x"}, "结果")


def test_non_forbidden_keys_pass() -> None:
    """普通字段不受影响——避免规则过宽导致正常摘要被拒。"""
    summary = normalize_summary({"symbol": "INTC", "as_of": "2026-09-01"}, "参数")
    assert set(summary) == {"symbol", "as_of"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    """``NaN`` / ``Infinity`` 不是合法 JSON，写入后无法回读。"""
    with pytest.raises(ValueError, match="有限数值"):
        normalize_summary({"count": value}, "参数")


def test_long_text_is_rejected() -> None:
    """超长文本必须拒绝：摘要是审计记录，不是数据通道。"""
    with pytest.raises(ValueError, match="文本过长"):
        normalize_summary({"note": "x" * 513}, "参数")


def test_text_at_the_limit_is_accepted() -> None:
    """恰好 512 字符通过。"""
    assert normalize_summary({"note": "x" * 512}, "参数")["note"] == "x" * 512


@pytest.mark.parametrize(
    "value", [{"nested": 1}, ["a"], object()]
)
def test_nested_values_are_rejected(value: object) -> None:
    """只接受扁平标量——嵌套结构是原始响应漏进摘要的典型形态。"""
    with pytest.raises(ValueError, match="扁平标量"):
        normalize_summary({"payload": value}, "结果")


def test_too_many_fields_is_rejected() -> None:
    """字段数上限防止把整份响应塞进摘要。"""
    with pytest.raises(ValueError, match="最多包含 32 个字段"):
        normalize_summary({f"k{index}": 1 for index in range(33)}, "结果")


def test_field_count_at_the_limit_is_accepted() -> None:
    """恰好 32 个字段通过。"""
    summary = normalize_summary({f"k{index}": 1 for index in range(32)}, "结果")
    assert len(summary) == 32


@pytest.mark.parametrize("key", ["", "x" * 65])
def test_invalid_key_length_is_rejected(key: str) -> None:
    """字段名长度必须在 1..64。"""
    with pytest.raises(ValueError, match="字段名长度无效"):
        normalize_summary({key: 1}, "参数")


# --------------------------------------------------------------------------
# 授权判定
# --------------------------------------------------------------------------

KNOWN = frozenset({"summary", "trades", "positions"})
CONFIGURED = frozenset({"summary", "trades"})


def test_known_and_configured_tool_is_allowed() -> None:
    """已知且已启用 → 放行。"""
    assert authorize_tool_call(
        tool_name="summary", known_tools=KNOWN, configured_tools=CONFIGURED
    ) is None


def test_unknown_tool_is_rejected() -> None:
    """模型编造的工具名必须被拒。"""
    assert authorize_tool_call(
        tool_name="delete_everything", known_tools=KNOWN, configured_tools=CONFIGURED
    ) == "unknown_tool"


def test_known_but_unconfigured_tool_is_rejected() -> None:
    """目录里有但当前未启用，同样不放行——已知不等于可用。"""
    assert authorize_tool_call(
        tool_name="positions", known_tools=KNOWN, configured_tools=CONFIGURED
    ) == "unconfigured_tool"

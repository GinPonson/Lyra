"""工具参数校验测试。

核心是一条**反强转**断言：客户端提交 ``"5"`` 不能被当成整数 5 通过。

若能强转，参数类型的事实来源就变成服务端的宽松程度，而不是 schema；任何依赖
"这里一定是 int" 的下游代码都会在真实数据上炸。
"""

from __future__ import annotations

import pytest

from lyra.application.financial_tool_validation import (
    is_valid_financial_tool_arguments,
    validate_financial_tool_arguments,
)

# 与真实工具同形：可选账户、必填 as_of、限幅的 limit、枚举分组。
SCHEMA: dict = {
    "type": "object",
    "properties": {
        "account": {"type": "string", "minLength": 1},
        "asOf": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "groupBy": {"type": "string", "enum": ["underlying", "asset_type"]},
    },
    "required": ["asOf"],
    "additionalProperties": False,
}


def test_accepts_valid_arguments() -> None:
    """合法参数通过。"""
    assert is_valid_financial_tool_arguments(
        SCHEMA, {"asOf": "2026-09-01", "limit": 10, "groupBy": "underlying"}
    )


def test_accepts_minimal_arguments() -> None:
    """只给必填项也通过。"""
    assert is_valid_financial_tool_arguments(SCHEMA, {"asOf": "2026-09-01"})


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"asOf": "2026-09-01", "limit": "10"}, "整数被写成字符串"),
        ({"asOf": "2026-09-01", "limit": 10.5}, "带小数的数字不是整数"),
        ({"asOf": "2026-09-01", "limit": True}, "布尔冒充整数"),
        ({"asOf": "2026-09-01", "account": 123}, "字符串被写成数字"),
        ({"asOf": "2026-09-01", "groupBy": "asset"}, "枚举外的取值"),
        ({"asOf": "2026-09-01", "limit": 0}, "低于下限"),
        ({"asOf": "2026-09-01", "limit": 51}, "高于上限"),
        ({"asOf": "09/01/2026"}, "不符合日期模式"),
        ({"asOf": "2026-09-01", "unknown": 1}, "多余字段"),
        ({}, "缺少必填项"),
        ({"asOf": "2026-09-01", "extra": None}, "多余字段(空值)"),
    ],
)
def test_rejects_invalid_arguments(arguments: dict, reason: str) -> None:
    """**不做类型强转**：类型不符即拒绝。"""
    assert is_valid_financial_tool_arguments(SCHEMA, arguments) is False, reason


def test_whole_number_floats_are_valid_integers() -> None:
    """``10.0`` 通过整数校验——**JSON 只有一种数字类型**。

    JSON Schema 规范把 ``integer`` 定义为"小数部分为零的数字"，Ajv 同样接受
    ``10.0``。这不是宽松，而是与 TypeScript 侧保持一致的判定；把它当错误会导致
    两边对同一请求给出不同结论。
    """
    assert is_valid_financial_tool_arguments(
        SCHEMA, {"asOf": "2026-09-01", "limit": 10.0}
    ) is True


@pytest.mark.parametrize("arguments", ["asOf=2026-09-01", 5, None, ["2026-09-01"]])
def test_rejects_non_object_arguments(arguments: object) -> None:
    """参数必须是对象；数组与标量一律拒绝。"""
    assert is_valid_financial_tool_arguments(SCHEMA, arguments) is False


def test_validate_variant_raises_on_invalid() -> None:
    """校验函数在非法时抛错，合法时静默返回。"""
    validate_financial_tool_arguments(SCHEMA, {"asOf": "2026-09-01"})

    with pytest.raises(Exception):
        validate_financial_tool_arguments(SCHEMA, {"asOf": "2026-09-01", "limit": "10"})


def test_does_not_mutate_the_arguments() -> None:
    """**校验不做转换**：调用方拿到的仍是原样参数。"""
    arguments = {"asOf": "2026-09-01", "limit": 10}
    snapshot = dict(arguments)

    assert is_valid_financial_tool_arguments(SCHEMA, arguments) is True
    assert arguments == snapshot


def test_schema_is_reused_across_calls() -> None:
    """同一 schema 的校验器被复用，且结论稳定。"""
    for _ in range(5):
        assert is_valid_financial_tool_arguments(SCHEMA, {"asOf": "2026-09-01"}) is True
        assert is_valid_financial_tool_arguments(SCHEMA, {"asOf": "bad"}) is False


def test_empty_schema_accepts_anything_object_like() -> None:
    """空 schema 是合法的（没有参数的工具）。"""
    assert is_valid_financial_tool_arguments({}, {"anything": 1}) is True
    assert is_valid_financial_tool_arguments({}, {}) is True

"""对照器本身的元测试。

**为什么必须有这一层**：`test_analytics_parity.py` 永远拿真实夹具比对。如果
:func:`compare_structure` 有 bug（例如永远返回空差异、或路径前缀写错导致零交集），
那么 M3 全部 2771 个字段的验证都会失效——**而测试依然全绿**。

一个不会因差异而失败的比较器等于没有比较器。本文件通过**主动篡改期望值**证明
它真的会失败。
"""

from __future__ import annotations

import copy
import re

import pytest
from parity_support import (
    analytics_cases,
    canonical,
    compare_structure,
    flatten,
    normalize_scalar,
    to_plain,
)


def _fixture() -> dict[str, object]:
    """取第一条分析层语料。"""
    cases = analytics_cases()
    assert cases, "语料为空"
    return cases[0]


def _expected(command: str) -> object:
    """取某条命令的 oracle 期望值。"""
    fixture = _fixture()
    return fixture["expected"]["analytics"][command]  # type: ignore[index]


# --------------------------------------------------------------------------
# 基线工具的行为
# --------------------------------------------------------------------------


def test_normalize_scalar_treats_equal_numbers_as_equal() -> None:
    """同一个小数的不同渲染位数必须视为相同（比值不比串）。"""
    assert normalize_scalar("5.000000") == normalize_scalar("5")
    assert normalize_scalar("-865.50") == normalize_scalar("-865.5")
    assert normalize_scalar("0.0000") == "0"
    assert normalize_scalar("-0.0000") == "0"
    assert normalize_scalar("5.000000") != normalize_scalar("5.000001")


def test_normalize_scalar_keeps_dates_and_hashes_intact() -> None:
    """日期与哈希不得被数值归一破坏。"""
    assert normalize_scalar("2026-01-05") == "2026-01-05"
    assert normalize_scalar("6b0f02d7") == "6b0f02d7"
    assert normalize_scalar(None) == "<NULL>"


def test_canonical_is_order_insensitive() -> None:
    """行序不同不影响判定（比集合不比序）。"""
    rows = [["a", "1"], ["b", "2"]]
    assert canonical(rows) == canonical(list(reversed(rows)))


def test_to_plain_preserves_decimal() -> None:
    """``to_plain`` 必须保留 Decimal 对象，否则数值归一无法生效。"""
    from decimal import Decimal

    plain = to_plain({"amount": Decimal("5.000000")})
    assert isinstance(plain, dict)
    assert isinstance(plain["amount"], Decimal)


def test_flatten_folds_arrays_into_field_multisets() -> None:
    """记录数组按字段拉平，既能比对内容又不受行序影响。"""
    left: dict[str, str] = {}
    right: dict[str, str] = {}
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    flatten({"rows": rows}, "$", left)
    flatten({"rows": list(reversed(rows))}, "$", right)

    assert left == right
    assert "$.rows.a" in left


# --------------------------------------------------------------------------
# 对照器必须真的会失败
# --------------------------------------------------------------------------


def test_control_unmodified_fixture_passes() -> None:
    """对照组：未篡改的期望值必须通过比对。"""
    command = "summary"
    fixture = _fixture()
    actual = _expected(command)
    mismatches, compared, _ = compare_structure(
        _expected(command), to_plain(actual), minimum_leaves=4, label_prefix=command
    )

    assert mismatches == []
    assert compared > 0, f"{fixture['caseId']}: 比对字段数为 0"


def test_detects_changed_value() -> None:
    """篡改一个数值后必须报告差异。"""
    command = "summary"
    oracle = copy.deepcopy(_expected(command))
    assert isinstance(oracle, dict)
    oracle["open_positions"] = int(oracle["open_positions"]) + 7  # type: ignore[arg-type]

    mismatches, _, _ = compare_structure(
        oracle, to_plain(_expected(command)), minimum_leaves=4, label_prefix=command
    )

    assert mismatches, "篡改数值后比较器仍然通过——对照器已失效"
    assert any("open_positions" in item for item in mismatches)


def test_detects_changed_text_value() -> None:
    """篡改文本值同样必须被发现（不要只测数字）。"""
    command = "summary"
    oracle = copy.deepcopy(_expected(command))
    assert isinstance(oracle, dict)
    oracle["date_range"] = "1999-01-01 ~ 1999-12-31"

    mismatches, _, _ = compare_structure(
        oracle, to_plain(_expected(command)), minimum_leaves=4, label_prefix=command
    )

    assert mismatches


def test_detects_missing_row_in_array() -> None:
    """数组少一行必须被发现（多重集比较不能只看集合大小）。"""
    command = "trades"
    oracle = copy.deepcopy(_expected(command))
    assert isinstance(oracle, dict)
    rows = oracle.get("rows")
    if not isinstance(rows, list) or len(rows) < 2:
        pytest.skip("该语料的 trades 行数不足以删除一行")

    oracle["rows"] = rows[:-1]
    mismatches, _, _ = compare_structure(
        oracle, to_plain(_expected(command)), minimum_leaves=1, label_prefix=command
    )

    assert mismatches


def test_empty_comparison_is_flagged_as_insufficient_coverage() -> None:
    """键名全部对不上时必须报「覆盖不足」，而不是静默通过。"""
    command = "summary"
    oracle = {"totallyDifferentField": 1}

    mismatches, compared, _ = compare_structure(
        oracle, to_plain(_expected(command)), minimum_leaves=4, label_prefix=command
    )

    assert compared == 0
    assert mismatches, "零交集时比较器仍然通过——空比对假通过"
    assert any("覆盖不足" in item for item in mismatches)


def test_coverage_floor_cannot_exceed_oracle_leaves() -> None:
    """下限不得超过 oracle 自身提供的字段数；否则空账本会误报。

    注意两侧键名必须一致：Python 侧对照器**刻意不做键名归一**——领域类型直接
    采用 oracle 的键名，因此归一反而会掩盖命名差异。
    """
    oracle = {"only_field": 1}

    mismatches, compared, _ = compare_structure(
        oracle, to_plain({"only_field": 1}), minimum_leaves=99, label_prefix="x"
    )

    # 只有 1 个字段可比，下限被压到 1，因此不报覆盖不足。
    assert compared == 1
    assert mismatches == []


def test_key_names_are_not_normalized() -> None:
    """键名不做归一：命名不同即视为不可比，而不是靠规则猜它们同义。"""
    mismatches, compared, _ = compare_structure(
        {"onlyField": 1}, to_plain({"only_field": 1}), minimum_leaves=1, label_prefix="x"
    )

    assert compared == 0
    assert mismatches, "命名不同却判为相同——键名归一掩盖了差异"


def test_whitelist_suppresses_known_difference_only() -> None:
    """白名单只放行命中的路径，其余差异仍然报错。"""
    command = "summary"
    oracle = copy.deepcopy(_expected(command))
    assert isinstance(oracle, dict)
    oracle["date_range"] = "changed"
    oracle["open_positions"] = int(oracle["open_positions"]) + 1  # type: ignore[arg-type]

    mismatches, _, whitelisted = compare_structure(
        oracle,
        to_plain(_expected(command)),
        minimum_leaves=4,
        label_prefix=command,
        ignored_paths=(re.compile(rf"^{command}\.date_range$"),),
    )

    assert whitelisted == [f"{command}.date_range"]
    assert len(mismatches) == 1
    assert "open_positions" in mismatches[0]


def test_label_prefix_appears_in_messages() -> None:
    """路径标签必须带命令前缀，否则白名单模式无法精确定位。"""
    command = "summary"
    oracle = copy.deepcopy(_expected(command))
    assert isinstance(oracle, dict)
    oracle["date_range"] = "changed"

    mismatches, _, _ = compare_structure(
        oracle, to_plain(_expected(command)), minimum_leaves=4, label_prefix=command
    )

    assert mismatches
    assert mismatches[0].startswith(f"{command}.")

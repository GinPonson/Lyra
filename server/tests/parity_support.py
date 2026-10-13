"""差分对照测试的共享支撑代码。

供 `test_ledger_parity.py` 与 `test_fifo_parity.py` 复用：读取与 TypeScript 侧
同源的 oracle 录制语料，并按同一套规则做归一化比较。

比较规则**记录在语料本身**（每条用例含输入与预期输出）：
比值不比串、比集合不比序、错误消息按数值归一后比较。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "parity-fixtures.json"

# 纯十进制按数值归一。
_DECIMAL_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")
_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")
# oracle 录制时带上了异常类名；比较前剥离。
_ERROR_TYPE_PREFIX = re.compile(r"^[A-Za-z]*(?:Error|Exception): ")

# oracle 用例通过故障注入模拟数据库失败，不属于口径差异。
# 与 TypeScript harness 的 SKIPPED_CASE_REASONS 保持同一份清单。
SKIPPED_CASE_IDS: frozenset[str] = frozenset({
    "tests/test_ingest.py::TestImport::test_database_failure_rolls_back_whole_file#csv#1",
    "tests/test_fifo.py::TestRebuildIdempotent::test_database_failure_preserves_previous_rebuild#fifo#2",
})


def load_cases() -> list[dict[str, object]]:
    """读取与 TypeScript 侧同源的对照语料。"""
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert isinstance(cases, list)
    return cases


def cases_of_kind(kind: str) -> list[dict[str, object]]:
    """按类型筛选语料，排除故障注入用例。

    csv 与 fifo 只取同一用例中的**首次**调用：第 2 次及以后落在累计账本状态上，
    复现它需要模拟账本本身，不在本次对照范围内。
    """
    selected = [
        case for case in load_cases()
        if case["kind"] == kind and str(case["caseId"]) not in SKIPPED_CASE_IDS
    ]
    if kind == "fifo":
        # 多文件重放涉及重叠快照与幂等语义，同样需要累计状态。
        selected = [case for case in selected if case["input"].get("fileCount") == 1]
    elif kind == "csv":
        selected = [case for case in selected if str(case["caseId"]).endswith("#csv#1")]
    return selected


def normalize_scalar(value: str | None) -> str:
    """把标量归一化为可比值；数字按数值归一，其余原样保留。"""
    if value is None:
        return "<NULL>"
    if _DECIMAL_PATTERN.fullmatch(value):
        number = Decimal(value)
        # 零统一为 "0"，避免 -0 与 0 被判为不同。
        return "0" if number == 0 else format(number.normalize(), "f")
    return value


def canonical(rows: list[list[str | None]]) -> list[str]:
    """把行集合归一化为可排序、可比较的多重集（行序无契约）。"""
    return sorted(
        "\u0001".join(normalize_scalar(cell) for cell in row)
        for row in rows
    )


def normalize_message(text: str) -> str:
    """归一化错误消息里的数值文本，避免格式差异被误报为口径差异。"""
    return _NUMBER_PATTERN.sub(lambda match: normalize_scalar(match.group()), text)


def expected_error_text(case: dict[str, object]) -> str:
    """取出 oracle 的失败原因并剥离异常类名前缀。"""
    return _ERROR_TYPE_PREFIX.sub("", str(case["expectedError"]))


def first_call_case_id(case: dict[str, object]) -> str:
    """返回用例标识，便于在断言消息里定位。"""
    return str(case["caseId"])


def analytics_cases() -> list[dict[str, object]]:
    """取出分析层语料（19 份账本 × 16 条命令）。"""
    return [case for case in load_cases() if case["kind"] == "analytics"]


# ---------------------------------------------------------------------------
# 结构化对照：把两侧都扁平化为「路径 → 归一化值」，再比较交集。
#
# 与 TypeScript 侧 parity-harness.ts 采用同一套规则：
# 数值按数值比较、记录数组按字段拉平成多重集（吸收行序差异），
# 只在单侧存在的路径视为呈现层差异不计失败，但由调用方加最小覆盖数闸门
# 防止「键名全对不上」造成的空比对假通过。
# ---------------------------------------------------------------------------


def _scalar_text(value: object) -> str:
    """把标量渲染为稳定文本。"""
    if isinstance(value, bool):
        # 与 JSON 的 true/false 对齐，避免 Python 的 True/False 造成假差异。
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, int | float):
        return str(value)
    return str(value)


def _serialize(value: object) -> str:
    """把一个元素序列化为 ``键=值`` 的排序串。"""
    inner: dict[str, str] = {}
    flatten(value, "", inner)
    return ";".join(f"{key}={inner[key]}" for key in sorted(inner))


def flatten(value: object, prefix: str, out: dict[str, str]) -> None:
    """把任意结构扁平化为「路径 → 归一化标量」。数组折叠为排序后多重集。"""
    if value is None:
        out[prefix] = "<NULL>"
        return
    if isinstance(value, (list, tuple)):
        items = list(value)
        if items and all(isinstance(item, dict) for item in items):
            fields: set[str] = set()
            for item in items:
                fields.update(item.keys())  # type: ignore[union-attr]
            for field in fields:
                parts = sorted(
                    _serialize(item.get(field))  # type: ignore[union-attr]
                    for item in items
                )
                out[f"{prefix}.{field}"] = "[" + " || ".join(parts) + "]"
            return
        parts = sorted(_serialize(item) for item in items)
        out[prefix] = "[" + " || ".join(parts) + "]"
        return
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flatten(child, path, out)
        return
    out[prefix] = normalize_scalar(_scalar_text(value))


def to_plain(value: object) -> object:
    """把 dataclass 与元组递归转换为可扁平化的普通结构。

    ``Decimal`` 必须原样保留：它的字符串形式会保留标度，交给
    :func:`normalize_scalar` 做数值归一才能真正比值而不是比串。
    """
    if hasattr(value, "__dataclass_fields__"):
        return {
            field: to_plain(getattr(value, field))
            for field in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain(child) for key, child in value.items()}
    return value


def compare_structure(
    oracle: object,
    actual: object,
    *,
    minimum_leaves: int,
    label_prefix: str = "",
    ignored_paths: tuple[re.Pattern[str], ...] = (),
) -> tuple[list[str], int, list[str]]:
    """比较两侧结构，返回差异列表、比对字段数与白名单命中路径。

    @param oracle: oracle 的期望值。
    @param actual: Python 侧结果（已由 :func:`to_plain` 转换）。
    @param minimum_leaves: 至少要比对到的叶字段数，低于此值视为空比对。
    @param label_prefix: 路径标签前缀（通常是命令名），便于定位与写白名单。
    @param ignored_paths: 已知差异的路径模式；命中即放行并记录。
    @returns ``(差异列表, 比对字段数, 白名单命中)``。
    """
    oracle_flat: dict[str, str] = {}
    actual_flat: dict[str, str] = {}
    flatten(to_plain(oracle), "$", oracle_flat)
    flatten(to_plain(actual), "$", actual_flat)

    mismatches: list[str] = []
    whitelisted: list[str] = []
    compared = 0
    for path, expected in oracle_flat.items():
        if path not in actual_flat:
            continue
        compared += 1
        if actual_flat[path] == expected:
            continue
        label = f"{label_prefix}{path[1:] or path}"
        if any(pattern.match(label) for pattern in ignored_paths):
            whitelisted.append(label)
            continue
        mismatches.append(f"{label}: oracle={expected} python={actual_flat[path]}")

    if compared < min(minimum_leaves, len(oracle_flat)):
        mismatches.append(
            f"比对覆盖不足: 仅比到 {compared} 项，低于下限 {min(minimum_leaves, len(oracle_flat))}"
        )
    return mismatches, compared, whitelisted


"""账本来源类型与 schema 约束的一致性测试。

**这条测试的存在理由**：`LedgerSourceType` 曾写成 `Literal["csv", "xlsx", "gmail"]`，
而 schema 的 CHECK 是 `('csv', 'excel', 'gmail')`。

**没有任何测试能发现它**——因为当时没有任何代码路径真的以 `"xlsx"` 写入 `source_type`。
缺陷是潜伏的：一旦有人补上 Excel 解析并顺手用 `"xlsx"`，报错会发生在数据库层，
与"为什么这里叫 xlsx"这个直觉性的假设相去甚远。

因此这里**直接把类型与 schema 文本对照**，而不是测某个具体调用。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from lyra.domain.ledger.types import LedgerSourceType

SCHEMA_PATH = Path("src/lyra/db/sql/schema.sql")


def _schema_source_types() -> set[str]:
    """从 schema 文本里取出 ``source_type`` 的允许值。"""
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"source_type\s+VARCHAR\s+NOT\s+NULL\s+CHECK\s*\(\s*source_type\s+IN\s*\(([^)]*)\)",
        text,
    )
    assert match is not None, "未能在 schema 中找到 source_type 的 CHECK 约束"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_python_type_matches_the_schema_check() -> None:
    """**Python 的 Literal 必须与 schema 的 CHECK 完全一致。**

    不一致时，写入会在数据库层失败——而错误信息只会说"违反约束"，
    不会告诉调用方"你该用 excel 而不是 xlsx"。
    """
    assert set(get_args(LedgerSourceType)) == _schema_source_types()


def test_excel_is_the_source_type_not_xlsx() -> None:
    """来源类型是 ``excel``（容器无关），而不是 ``xlsx``。"""
    types = set(get_args(LedgerSourceType))

    assert "excel" in types
    assert "xlsx" not in types


def test_upload_file_type_is_separate_from_source_type() -> None:
    """**上传文件类型与账本来源类型同名不同义。**

    - 上传类型按扩展名取值（``xlsx``）；
    - 来源类型按容器类型取值（``excel``）。

    两者被混用时会写出 `source_type="xlsx"`，撞上 schema 约束。
    """
    from lyra.application.import_upload import ImportFileType

    assert set(get_args(ImportFileType)) == {"csv", "xlsx"}
    # 两者确实不同——这正是容易混用的地方。
    assert set(get_args(ImportFileType)) != set(get_args(LedgerSourceType))


def test_gmail_is_still_a_valid_source_type() -> None:
    """Gmail 仍是合法来源。"""
    assert "gmail" in set(get_args(LedgerSourceType))

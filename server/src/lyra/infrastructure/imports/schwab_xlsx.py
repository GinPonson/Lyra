"""Schwab Transactions XLSX 解析。

镜像 TypeScript 版 ``infrastructure/imports/schwab-xlsx.ts``。

**表头必须出现在工作表的首个非空行。** 不在首个非空行就判定该表不是交易表——而不是
继续往下找。继续找会在一份"前面有标题行、后面才是数据"的表里选中它，而那些标题行
与交易无关。

**三类单元格结构被显式拒绝**：

| 结构 | 不拒绝会怎样 |
| --- | --- |
| **公式** | 导入的是公式而非其值；Excel 重算后金额可能与账本不一致 |
| **合并单元格** | 值只在左上角，其余格为空——行会缺列 |
| **隐藏工作表 / 隐藏行** | 用户看不到被导入的数据，而"我看到的工作表就是导入的工作表"是最基本的预期 |

**多个候选工作表时报歧义而不是挑第一个**——挑错了会让用户在一个他以为正确的文件上
得到错误的账本数据。
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

from ...domain.ledger.batch import TransactionBatchInput
from ...domain.ledger.errors import IngestError
from ...domain.ledger.parsers import parse_account
from ...domain.ledger.types import LedgerSourceType, RawTransactionFields
from .schwab_table import (
    FIELD_COUNT,
    create_schwab_unknown_headers_error,
    is_schwab_transaction_headers,
    map_schwab_transaction_row,
)


def parse_schwab_xlsx(
    content: bytes,
    *,
    file_name: str,
    source_hash: str,
    source_reference: str,
    source_type: LedgerSourceType = "excel",
) -> TransactionBatchInput:
    """严格解析 Schwab Transactions XLSX。

    @param content: 文件字节。
    @param file_name: 仅文件名，用于提取账户标识与错误定位。
    @param source_hash: 文件内容摘要。
    @param source_reference: 来源引用。
    @param source_type: 来源类型，默认 ``excel``（**不是 ``xlsx``**，见 types.py）。
    @returns 可直接交给批次标准化的输入。
    @raises IngestError: 工作簿无法读取、工作表歧义、单元格结构非法或行数不符。
    """
    import io
    import zipfile

    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        workbook = openpyxl.load_workbook(
            # read_only 会跳过样式等信息，但仍能读到行/列与单元格值。
            io.BytesIO(content), read_only=True, data_only=False
        )
    except (
        InvalidFileException, zipfile.BadZipFile, KeyError, OSError,
        ValueError, TypeError,
    ) as error:
        # 加密、损坏或根本不是 xlsx 都会走到这里。
        raise IngestError(f"{file_name}: XLSX 工作簿无法读取或已加密") from error

    try:
        candidates = [
            located for located in (
                _find_transaction_worksheet(sheet) for sheet in workbook.worksheets
            )
            if located is not None
        ]
        hidden = next((c for c in candidates if c[2] != "visible"), None)
        if hidden is not None:
            raise IngestError(
                f"{file_name}: 交易工作表不可隐藏（工作表 {hidden[0].title!r}）"
            )
        visible = [c for c in candidates if c[2] == "visible"]
        if not visible:
            # 参考实现这里传的是空列表而非实际工作表名。
            raise create_schwab_unknown_headers_error(())
        if len(visible) > 1:
            raise IngestError(
                f"{file_name}: 存在多个 Schwab Transactions 候选工作表: "
                f"{[c[0].title for c in visible]}"
            )
        sheet, header_row, _ = visible[0]
        rows = _read_transaction_rows(file_name, sheet, header_row)
    finally:
        workbook.close()

    return TransactionBatchInput(
        file_name=file_name,
        account=parse_account(file_name),
        source_hash=source_hash,
        source_type=source_type,
        source_reference=source_reference,
        rows=tuple(rows),
    )


def _find_transaction_worksheet(worksheet: object) -> tuple[object, int, str] | None:
    """判断一个工作表是否为交易表。

    只看**首个非空行**：它是表头就选中，否则判定不是交易表（**不继续往下找**）。

    @returns ``(工作表, 表头行号, 可见性)``；不是交易表时为 ``None``。
    """
    for row_number, values in enumerate(worksheet.iter_rows(values_only=True), start=1):
        texts = _row_texts(values)
        if not any(text != "" for text in texts):
            # 完全空的行跳过，继续看下一行。
            continue
        # 首个非空行就是判定点——不是表头即返回 None。
        return (
            (worksheet, row_number, getattr(worksheet, "sheet_state", "visible"))
            if is_schwab_transaction_headers(texts)
            else None
        )
    return None


def _read_transaction_rows(
    file_name: str, worksheet: object, header_row: int
) -> list[RawTransactionFields]:
    """读取表头之后的数据行。

    @raises IngestError: 隐藏行、公式或合并单元格、列数不符。
    """
    hidden_rows = _hidden_row_numbers(worksheet)
    rows: list[RawTransactionFields] = []
    for row_number, row in enumerate(worksheet.iter_rows(), start=1):
        if row_number <= header_row:
            continue
        cells = list(row)
        texts = _cell_texts(cells, file_name, worksheet.title, row_number)
        if not any(text != "" for text in texts):
            # 空行跳过——**隐藏的空行同样跳过**：参考实现先判空、再判隐藏，
            # 而 Excel 常常留下被隐藏的空白格式行。
            continue
        if row_number in hidden_rows:
            raise IngestError(
                f"{file_name} 工作表 {worksheet.title} 第{row_number}行: 交易行不可隐藏"
            )
        rows.append(map_schwab_transaction_row(
            tuple(texts), f"{file_name} 工作表 {worksheet.title} 第{row_number}行"
        ))
    return rows


def _hidden_row_numbers(worksheet: object) -> set[int]:
    """取出被隐藏的物理行号。

    read_only 模式丢弃行维度（``worksheet.row_dimensions`` 不可用），因此隐藏行一度
    完全检测不到。**不改为非 read_only 加载**：上传上限是 512MB，把整本工作簿展开成
    带样式的 Cell 对象会成倍占用内存，而这里只需要一个行号集合——行 XML 上的
    ``hidden`` 属性仍在，流式扫一遍该工作表即可，内存 O(1)。

    @returns 被隐藏的行号集合。
    """
    reader = getattr(worksheet, "_get_source", None)
    if reader is None:  # pragma: no cover - 仅在加载模式被改动时走到
        return {
            int(number)
            for number, dimension in getattr(worksheet, "row_dimensions", {}).items()
            if getattr(dimension, "hidden", False)
        }

    from xml.etree.ElementTree import iterparse

    hidden: set[int] = set()
    with reader() as source:
        for _, element in iterparse(source, events=("start",)):
            if element.tag.rsplit("}", 1)[-1] != "row":
                continue
            row_number = element.get("r")
            if element.get("hidden") in ("1", "true") and row_number is not None:
                hidden.add(int(row_number))
            # 只取属性；立刻释放，避免把整表 XML 留在内存里。
            element.clear()
    return hidden


def _row_texts(values: tuple) -> list[str]:
    """把一行原始值转为文本并裁掉尾部空列。"""
    texts = ["" if value is None else str(value) for value in values]
    while len(texts) > FIELD_COUNT and texts[-1] == "":
        texts.pop()
    return texts


def _cell_texts(
    cells: list, file_name: str, sheet_title: str, row_number: int
) -> list[str]:
    """把一行单元格转为文本。

    @raises IngestError: 公式、合并单元格或非法数值/日期。
    """
    texts: list[str] = []
    for cell in cells:
        texts.append(_cell_to_text(cell, file_name, sheet_title, row_number))
    while len(texts) > FIELD_COUNT and texts[-1] == "":
        texts.pop()
    return texts


def _cell_to_text(cell: object, file_name: str, sheet_title: str, row_number: int) -> str:
    """把单个单元格转为文本。

    @raises IngestError: 公式、合并单元格或非法值。
    """
    where = f"{file_name} 工作表 {sheet_title} 第{row_number}行 {getattr(cell, 'coordinate', '?')}"
    # **公式**：导入公式本身没有意义，而它的值会随 Excel 重算而变。
    if isinstance(getattr(cell, "value", None), str) and str(cell.value).startswith("="):
        raise IngestError(f"{where}: 不允许公式或合并单元格")
    # **合并单元格**：值只在左上角，其余格为空——行会缺列。
    if getattr(cell, "coordinate", None) is not None and _is_merged(cell):
        raise IngestError(f"{where}: 不允许公式或合并单元格")

    value = getattr(cell, "value", None)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # 布尔不是金额也不是文本；当成 "True"/"False" 会污染字段。
        raise IngestError(f"{where}: 单元格类型不支持")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise IngestError(f"{where}: 单元格数值无效")
        return _js_number_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        return f"{value.month:02d}/{value.day:02d}/{value.year}"
    if isinstance(value, date):
        return f"{value.month:02d}/{value.day:02d}/{value.year}"
    raise IngestError(f"{where}: 单元格类型不支持")


def _js_number_text(value: int | float) -> str:
    """把数值单元格渲染为与 ExcelJS ``cell.text`` 一致的文本。

    ExcelJS 的 ``cell.text`` 对数值就是 JS ``Number.prototype.toString()``
    （**不套用单元格的数字格式**），它与 Python ``str()`` 有两处会改变业务哈希的差异：

    | 值 | JS | Python ``str()`` |
    | --- | --- | --- |
    | ``100.0`` | ``100`` | ``100.0`` |
    | ``1e16`` | ``10000000000000000`` | ``1e+16`` |
    | ``1e-7`` | ``1e-7`` | ``1e-07`` |

    小数位取自源文本并进入 ``transaction_hash``，因此这些差异会让同一份文件在迁移
    前后算出不同的哈希、重复导入时产生重复交易。

    @param value: 单元格数值（非有限值已在上游拒绝）。
    @returns 与 JS 数值文本一致的十进制文本。
    """
    if isinstance(value, int):
        return str(value)
    # JS 对整数值的浮点不带 ``.0``；1e21 起改用指数形式。
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))

    magnitude = Decimal(repr(value))
    if Decimal("1e-6") <= abs(magnitude) < Decimal("1e21"):
        # JS 在 1e-6（含）以上、1e21 以下用定点形式。
        return format(magnitude, "f")

    sign, digits, exponent = magnitude.normalize().as_tuple()
    leading = exponent + len(digits) - 1
    mantissa = str(digits[0])
    if len(digits) > 1:
        mantissa += "." + "".join(str(digit) for digit in digits[1:])
    return f"{'-' if sign else ''}{mantissa}e{'+' if leading >= 0 else '-'}{abs(leading)}"


def _is_merged(cell: object) -> bool:
    """判断单元格是否属于合并区域。"""
    return bool(getattr(cell, "coordinate", None)) and (
        getattr(cell, "value", None) is not None
        and getattr(getattr(cell, "parent", None), "merged_cells", None) is not None
        and any(
            cell.coordinate in merged
            for merged in getattr(cell.parent, "merged_cells", None).ranges
        )
    )

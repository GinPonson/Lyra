"""上传字节到预览的组装测试。

覆盖三步组装：**上传边界校验 → CSV 解析 → 预览生成**。重点是三处容易做漏的地方：

1. **超限在接收时就抛错**，不是把整个文件读进内存后才发现；
2. **`.xlsx` 得到显式的 501**，而不是被伪装成"格式不支持"或静默失败——
   TypeScript 侧有 Excel 解析，Python 侧尚未移植，这是缺口而非设计选择；
3. **预览里的跳过数来自真实账本哈希**，不是猜的。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_prepare import MAX_IMPORT_BYTES, prepare_import_preview
from lyra.domain.ledger.errors import IngestError

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
FILE_NAME = "Individual_ABC123_Transactions_20260105.csv"

HEADER = "Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\n"


def _csv(*rows: str) -> bytes:
    """拼一份完整 CSV。"""
    return (HEADER + "".join(f"{row}\n" for row in rows)).encode("utf-8")


BUY = '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"'
SELL = '"01/10/2026","Sell","INTC","INTEL CORP","10","$60.00","","$600.00"'


def _xlsx_content() -> bytes:
    """生成一份含一行交易的真实工作簿。"""
    import io

    import openpyxl

    from lyra.infrastructure.imports.schwab_table import SCHWAB_TRANSACTION_HEADERS

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Transactions"
    sheet.append(list(SCHWAB_TRANSACTION_HEADERS))
    sheet.append(["01/05/2026", "Buy", "INTC", "INTEL CORP", "10", "$40.5", "", "-$405"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _prepare(content: bytes, **overrides):
    """以默认参数调用组装。"""
    arguments = {
        "file_name": FILE_NAME,
        "content": content,
        "parser_version": "2",
        "base_ledger_version": 0,
        "existing_transaction_hashes": frozenset(),
        "now": NOW,
    }
    arguments.update(overrides)
    return prepare_import_preview(**arguments)


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_prepares_preview_from_csv_bytes() -> None:
    """合法 CSV 应产出元数据、解析批次与预览。"""
    prepared = _prepare(_csv(BUY, SELL))

    assert prepared.file_name == FILE_NAME
    assert prepared.file_type == "csv"
    assert prepared.size_bytes == len(_csv(BUY, SELL))
    assert len(prepared.file_hash) == 64
    assert prepared.preview.total_rows == 2
    assert prepared.preview.insertable_rows == 2
    assert prepared.preview.first_transaction_date == "2026-01-05"
    assert prepared.preview.last_transaction_date == "2026-01-10"
    assert prepared.batch.account


def test_file_hash_matches_the_bytes() -> None:
    """文件哈希必须对应真实字节，供审计与去重使用。"""
    import hashlib

    content = _csv(BUY)
    prepared = _prepare(content)

    assert prepared.file_hash == hashlib.sha256(content).hexdigest()


def test_existing_hashes_reduce_insertable_rows() -> None:
    """已存在的交易计入跳过数。"""
    first = _prepare(_csv(BUY, SELL))
    existing = frozenset(t.transaction_hash for t in first.preview.transactions)

    again = _prepare(_csv(BUY, SELL), existing_transaction_hashes=existing)

    assert again.preview.existing_rows == 2
    assert again.preview.insertable_rows == 0


# --------------------------------------------------------------------------
# 上传边界
# --------------------------------------------------------------------------


def test_rejects_oversized_upload() -> None:
    """超过上限必须拒绝。"""
    with pytest.raises(ApplicationError, match="大小上限"):
        _prepare(_csv(BUY), max_size_bytes=10)


def test_rejects_empty_upload() -> None:
    """空文件必须拒绝。"""
    with pytest.raises(ApplicationError, match="不能为空"):
        _prepare(b"")


def test_size_limit_default_is_recorded() -> None:
    """默认上限是一个明确的值，便于运维核对。"""
    assert MAX_IMPORT_BYTES == 512 * 1024 * 1024


def test_rejects_unsafe_file_name() -> None:
    """文件名含路径分隔符必须拒绝。"""
    with pytest.raises(ApplicationError, match="文件名无效"):
        _prepare(_csv(BUY), file_name="../evil.csv")


# --------------------------------------------------------------------------
# 格式
# --------------------------------------------------------------------------


def test_xlsx_is_parsed_with_the_xlsx_parser() -> None:
    """**`.xlsx` 现在走真实解析器**（此前返回 501 的缺口已闭合）。

    用一份真的工作簿验证：来源类型是 ``excel``，且账户与行都被正确读出。
    """
    batch = _prepare(_xlsx_content(), file_name="Individual_ABC123_Transactions.xlsx")

    assert batch.batch.source_type == "excel"
    assert batch.batch.account == "ABC123"
    assert len(batch.batch.rows) == 1
    assert batch.batch.rows[0].symbol == "INTC"


def test_corrupt_xlsx_reports_a_readable_error() -> None:
    """**不是工作簿的字节报"无法读取或已加密"**，而不是 501。

    501 的语义是"格式尚未实现"；这个文件是格式正确但内容损坏，两者对用户的
    下一步动作完全不同。
    """
    with pytest.raises(IngestError, match="无法读取或已加密"):
        _prepare(b"\x50\x4b\x03\x04" + b"\x00" * 64,
                 file_name="Individual_ABC123_Transactions.xlsx")


def test_unsupported_extension_is_rejected_before_parsing() -> None:
    """非 csv/xlsx 走格式不支持分支（415），与 501 区分开。"""
    with pytest.raises(ApplicationError) as raised:
        _prepare(b"whatever", file_name="ledger.pdf")

    assert raised.value.code == "IMPORT_UNSUPPORTED_FORMAT"
    assert raised.value.status_code == 415


# --------------------------------------------------------------------------
# 解析失败
# --------------------------------------------------------------------------


def test_wrong_headers_surface_as_ingest_error() -> None:
    """表头不符必须由解析层抛出，而不是被组装层吞掉。"""
    bad = b"Foo,Bar\n1,2\n"

    with pytest.raises(IngestError, match="表头不符"):
        _prepare(bad)


def test_unparsable_file_name_surfaces_as_ingest_error() -> None:
    """文件名不符合券商命名格式时无法解析账户，必须抛出。"""
    with pytest.raises(IngestError, match="无法从文件名解析账户"):
        _prepare(_csv(BUY), file_name="ledger.csv")


def test_amount_mismatch_is_rejected_by_the_normalizer() -> None:
    """金额勾稽不符必须由标准化层拒绝，不能写进预览。"""
    inconsistent = '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$999.00"'

    with pytest.raises(IngestError, match="金额勾稽不符"):
        _prepare(_csv(inconsistent))

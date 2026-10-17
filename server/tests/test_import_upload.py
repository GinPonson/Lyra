"""导入上传边界校验测试。

这些校验是**唯一**在创建临时文件与进入解析器之前拦下非法上传的地方，因此每条
拒绝路径都要有用例。重点是三条容易做漏的检查：

1. 扩展名与**实际签名**必须一致（`.csv` 声称是文本，实际是 ZIP/OLE 容器）；
2. CSV 开头不得含 NUL（含 NUL 就不是文本，作为文本导入只会产出垃圾交易）；
3. 文件名不得含路径分隔符（否则临时文件会落到预期目录之外）。
"""

from __future__ import annotations

import hashlib

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_upload import (
    DEFAULT_SAMPLE_SIZE_BYTES,
    LEGACY_XLS_MAGIC,
    XLSX_MAGIC,
    ImportUploadMonitor,
    inspect_import_upload,
)

MAX = 1024
CSV_BYTES = b"Date,Action,Symbol\n01/05/2026,Buy,INTC\n"


def _inspect(
    file_name: str = "ledger.csv",
    body: bytes = CSV_BYTES,
    *,
    size_bytes: int | None = None,
    max_size_bytes: int = MAX,
):
    """以「样本即全文」的方式调用校验。"""
    return inspect_import_upload(
        file_name=file_name,
        size_bytes=len(body) if size_bytes is None else size_bytes,
        leading_bytes=body,
        max_size_bytes=max_size_bytes,
    )


# --------------------------------------------------------------------------
# 大小与文件名
# --------------------------------------------------------------------------


def test_accepts_plain_csv() -> None:
    """普通 CSV 通过并把文件名规范化。"""
    result = _inspect("  ledger.csv  ")

    assert result.file_name == "ledger.csv"
    assert result.file_type == "csv"
    assert result.size_bytes == len(CSV_BYTES)


def test_accepts_xlsx_with_zip_signature() -> None:
    """带 ZIP 签名的 .xlsx 通过。"""
    result = _inspect("ledger.xlsx", XLSX_MAGIC + b"\x00" * 32)

    assert result.file_type == "xlsx"


@pytest.mark.parametrize(("size", "reason"), [(0, "空文件"), (-1, "负大小")])
def test_rejects_empty_or_negative_size(size: int, reason: str) -> None:
    """空文件与非法大小必须拒绝。"""
    with pytest.raises(ApplicationError) as raised:
        _inspect(size_bytes=size)
    assert raised.value.code == "IMPORT_VALIDATION_FAILED", reason


def test_rejects_oversized_file() -> None:
    """超过上限必须拒绝。"""
    with pytest.raises(ApplicationError, match="大小上限"):
        _inspect(size_bytes=MAX + 1)


def test_rejects_sample_larger_than_file() -> None:
    """样本长度大于文件大小说明计数与采样不一致。"""
    with pytest.raises(ApplicationError, match="不一致"):
        _inspect(body=CSV_BYTES, size_bytes=len(CSV_BYTES) - 1)


def test_rejects_empty_sample() -> None:
    """空样本必须拒绝。"""
    with pytest.raises(ApplicationError):
        inspect_import_upload(
            file_name="a.csv", size_bytes=10, leading_bytes=b"", max_size_bytes=MAX
        )


@pytest.mark.parametrize(
    "file_name",
    ["", "   ", "../evil.csv", "dir/evil.csv", "dir\\evil.csv", ".", ".."],
)
def test_rejects_unsafe_file_names(file_name: str) -> None:
    """文件名不得为空或含路径分隔符。"""
    with pytest.raises(ApplicationError, match="文件名无效"):
        _inspect(file_name)


def test_rejects_invalid_max_size_configuration() -> None:
    """上限配置非法是**服务端配置错误**，抛 ValueError 而不是 422。"""
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="正安全整数"):
            _inspect(max_size_bytes=bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 签名与扩展名一致性
# --------------------------------------------------------------------------


def test_rejects_csv_that_is_actually_zip() -> None:
    """`.csv` 实际是 ZIP 容器必须拒绝，不能当文本解析。"""
    with pytest.raises(ApplicationError, match="格式不一致"):
        _inspect("ledger.csv", XLSX_MAGIC + b"rest")


def test_rejects_csv_that_is_actually_legacy_xls() -> None:
    """`.csv` 实际是旧版 OLE 容器同样拒绝。"""
    with pytest.raises(ApplicationError, match="格式不一致"):
        _inspect("ledger.csv", LEGACY_XLS_MAGIC + b"rest")


def test_rejects_csv_containing_nul() -> None:
    """含 NUL 的「CSV」不是文本，必须拒绝。"""
    with pytest.raises(ApplicationError, match="NUL"):
        _inspect("ledger.csv", b"Date,Action\n\x00\x01\x02")


def test_nul_after_sample_window_is_not_detected() -> None:
    """NUL 只检查样本窗口内——超出窗口的部分由解析器负责。

    这条断言记录的是**已知边界**，不是理想行为：样本大小决定检测范围。
    """
    body = b"a" * DEFAULT_SAMPLE_SIZE_BYTES + b"\x00"
    result = inspect_import_upload(
        file_name="ledger.csv",
        size_bytes=len(body),
        leading_bytes=body[:DEFAULT_SAMPLE_SIZE_BYTES],
        max_size_bytes=len(body) + 1,
    )
    assert result.file_type == "csv"


def test_rejects_xlsx_without_zip_signature() -> None:
    """`.xlsx` 没有 ZIP 签名必须拒绝。"""
    with pytest.raises(ApplicationError, match="XLSX 文件签名无效"):
        _inspect("ledger.xlsx", b"Date,Action\n")


@pytest.mark.parametrize("file_name", ["ledger.pdf", "ledger", "ledger.csv.exe", "ledger.txt"])
def test_rejects_unsupported_extension(file_name: str) -> None:
    """非 csv/xlsx 一律 415，并告知支持格式。"""
    with pytest.raises(ApplicationError) as raised:
        _inspect(file_name)
    assert raised.value.code == "IMPORT_UNSUPPORTED_FORMAT"
    assert raised.value.status_code == 415
    assert raised.value.details == {"supported_formats": ["csv", "xlsx"]}


def test_extension_match_is_case_insensitive() -> None:
    """扩展名大小写不敏感。"""
    assert _inspect("LEDGER.CSV").file_type == "csv"
    assert _inspect("LEDGER.XLSX", XLSX_MAGIC + b"x").file_type == "xlsx"


# --------------------------------------------------------------------------
# 流式监控器
# --------------------------------------------------------------------------


def _monitor(max_size_bytes: int = MAX, sample_size_bytes: int = 16) -> ImportUploadMonitor:
    return ImportUploadMonitor("ledger.csv", max_size_bytes, sample_size_bytes)


def test_monitor_accumulates_hash_and_size() -> None:
    """摘要的哈希与大小必须覆盖全部字节。"""
    monitor = _monitor()
    monitor.feed(CSV_BYTES[:10])
    monitor.feed(CSV_BYTES[10:])
    monitor.finish()

    summary = monitor.summary()
    assert summary.size_bytes == len(CSV_BYTES)
    assert summary.sha256 == hashlib.sha256(CSV_BYTES).hexdigest()
    assert summary.file_type == "csv"


def test_monitor_sample_is_bounded() -> None:
    """头部样本不得超过配置的采样上限。"""
    monitor = _monitor(sample_size_bytes=8)
    monitor.feed(b"x" * 100)
    monitor.finish()

    assert len(monitor.summary().leading_bytes) == 8


def test_monitor_rejects_overflow_midstream() -> None:
    """超过上限必须**在接收时就**抛错，而不是等到最后。"""
    monitor = _monitor(max_size_bytes=10)
    monitor.feed(b"x" * 10)

    with pytest.raises(ApplicationError, match="大小上限"):
        monitor.feed(b"x")


def test_monitor_requires_finish_before_summary() -> None:
    """未结束的管道不能生成摘要——半读文件不能当作完整上传。"""
    monitor = _monitor()
    monitor.feed(CSV_BYTES)

    with pytest.raises(RuntimeError, match="尚未成功结束"):
        monitor.summary()


def test_monitor_rejects_data_after_finish() -> None:
    """结束后不得再接收数据。"""
    monitor = _monitor()
    monitor.feed(CSV_BYTES)
    monitor.finish()

    with pytest.raises(RuntimeError, match="已结束"):
        monitor.feed(b"more")


@pytest.mark.parametrize(("max_size", "sample"), [(0, 16), (16, 0), (1.5, 16), (16, 1.5)])
def test_monitor_rejects_invalid_configuration(max_size: object, sample: object) -> None:
    """上限与采样大小都必须是正整数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        ImportUploadMonitor("a.csv", max_size, sample)  # type: ignore[arg-type]

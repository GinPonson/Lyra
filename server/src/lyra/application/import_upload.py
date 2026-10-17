"""导入上传边界校验。

镜像 TypeScript 版 ``import-upload.ts``。

**只识别扩展名、大小与文件签名**，不尝试解析 CSV 或 ZIP：真正的内容解析仍必须
交给标准库，并复用统一八列标准化内核。这里做的是把明显不合法的上传挡在创建临时
文件和进入解析器之前。

三处安全检查值得单独说明：

1. **签名与扩展名必须一致**——`.csv` 实际是 ZIP/OLE 容器时拒绝，避免把二进制
   当文本喂给解析器；
2. **CSV 开头不得含 NUL**——含 NUL 说明不是文本，作为文本导入只会产出垃圾交易；
3. **文件名不得含路径分隔符**——否则临时文件会落到预期目录之外。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from .app_error import ApplicationError

ImportFileType = Literal["csv", "xlsx"]

# XLSX 是 ZIP 容器，正常工作簿必须以 local file header 开始。
XLSX_MAGIC = b"\x50\x4b\x03\x04"
# 旧版二进制 XLS 使用的 OLE Compound File 标识。
LEGACY_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# 足以识别文件签名且不会显著占用内存的默认样本大小。
DEFAULT_SAMPLE_SIZE_BYTES = 4096

_EXTENSION = re.compile(r"\.([^.]+)$")
_PATH_SEPARATORS = re.compile(r"[/\\]")


@dataclass(frozen=True)
class InspectedImportUpload:
    """已通过边界校验的上传描述。"""

    file_name: str
    file_type: ImportFileType
    size_bytes: int


@dataclass(frozen=True)
class ImportUploadSummary:
    """上传管道成功结束后的完整摘要。"""

    file_name: str
    file_type: ImportFileType
    size_bytes: int
    sha256: str
    leading_bytes: bytes


def inspect_import_upload(
    *,
    file_name: str,
    size_bytes: int,
    leading_bytes: bytes,
    max_size_bytes: int,
) -> InspectedImportUpload:
    """校验上传边界并判定文件类型。

    @param file_name: 客户端提供的原始文件名。
    @param size_bytes: 流式计数得到的实际大小。
    @param leading_bytes: 文件开头的有界样本。
    @param max_size_bytes: 配置的大小上限。
    @returns 规范化的文件名、判定类型与大小。
    @raises ValueError: 上限配置非法。
    @raises ApplicationError: 内容或文件名不合法（422），或格式不受支持（415）。
    """
    if not isinstance(max_size_bytes, int) or isinstance(max_size_bytes, bool) or max_size_bytes <= 0:
        raise ValueError("导入文件大小上限必须是正安全整数")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise _validation_error("导入文件不能为空")
    if size_bytes > max_size_bytes:
        raise _validation_error("导入文件超过允许的大小上限")
    if len(leading_bytes) == 0 or len(leading_bytes) > size_bytes:
        raise _validation_error("导入文件开头样本与文件大小不一致")

    normalized_name = file_name.strip()
    if (
        normalized_name == ""
        or _PATH_SEPARATORS.search(normalized_name)
        or normalized_name in (".", "..")
    ):
        raise _validation_error("导入文件名无效")

    match = _EXTENSION.search(normalized_name)
    extension = match.group(1).lower() if match else ""
    leading = bytes(leading_bytes)

    if extension == "xlsx":
        if not _starts_with(leading, XLSX_MAGIC):
            raise _validation_error("XLSX 文件签名无效")
        return InspectedImportUpload(
            file_name=normalized_name, file_type="xlsx", size_bytes=size_bytes
        )
    if extension == "csv":
        if _starts_with(leading, XLSX_MAGIC) or _starts_with(leading, LEGACY_XLS_MAGIC):
            raise _validation_error("CSV 扩展名与实际文件格式不一致")
        if b"\x00" in leading:
            raise _validation_error("CSV 开头包含 NUL 字节，无法作为文本导入")
        return InspectedImportUpload(
            file_name=normalized_name, file_type="csv", size_bytes=size_bytes
        )

    raise ApplicationError(
        code="IMPORT_UNSUPPORTED_FORMAT",
        message="仅支持 .csv 和 .xlsx 导入文件",
        status_code=415,
        retryable=False,
        details={"supported_formats": ["csv", "xlsx"]},
    )


class ImportUploadMonitor:
    """在接收字节的同时执行硬大小限制、SHA-256 与头部采样。

    对应 TypeScript 的 ``ImportUploadMonitor``（Node ``Transform``）。Python 侧不含
    流式管道，因此改为显式 ``feed`` / ``finish``：**超限时立即抛错，调用方仍须
    删除此前的临时文件**。
    """

    def __init__(
        self,
        file_name: str,
        max_size_bytes: int,
        sample_size_bytes: int = DEFAULT_SAMPLE_SIZE_BYTES,
    ) -> None:
        """@param file_name: 原始文件名。@param max_size_bytes: 上传硬上限。
        @param sample_size_bytes: 头部采样上限。
        @raises ValueError: 上限或样本大小不是正整数。
        """
        if not isinstance(max_size_bytes, int) or isinstance(max_size_bytes, bool) or max_size_bytes <= 0:
            raise ValueError("导入文件大小上限必须是正安全整数")
        if (
            not isinstance(sample_size_bytes, int)
            or isinstance(sample_size_bytes, bool)
            or sample_size_bytes <= 0
        ):
            raise ValueError("导入文件样本大小必须是正安全整数")
        self._file_name = file_name
        self._max_size_bytes = max_size_bytes
        self._sample_size_bytes = sample_size_bytes
        self._hash = hashlib.sha256()
        self._size_bytes = 0
        self._sample = bytearray()
        self._finished = False

    @property
    def size_bytes(self) -> int:
        """已接收的字节数。"""
        return self._size_bytes

    def feed(self, chunk: bytes) -> None:
        """接收一块字节并累计。

        @param chunk: 二进制块。
        @raises ApplicationError: 超过大小上限。
        @raises RuntimeError: 管道已结束。
        """
        if self._finished:
            raise RuntimeError("导入上传流已结束，不能再接收数据")
        next_size = self._size_bytes + len(chunk)
        if next_size > self._max_size_bytes:
            raise _validation_error("导入文件超过允许的大小上限")
        remaining = self._sample_size_bytes - len(self._sample)
        if remaining > 0:
            self._sample.extend(chunk[:remaining])
        self._hash.update(chunk)
        self._size_bytes = next_size

    def finish(self) -> None:
        """标记上传管道成功结束。"""
        self._finished = True

    def summary(self) -> ImportUploadSummary:
        """生成摘要；**必须在上传成功结束后调用**。

        @returns 类型、大小、哈希与有界头部样本。
        @raises RuntimeError: 管道尚未结束——半读文件不能生成摘要。
        @raises ApplicationError: 边界校验失败。
        """
        if not self._finished:
            raise RuntimeError("导入上传流尚未成功结束，不能生成摘要")
        inspected = inspect_import_upload(
            file_name=self._file_name,
            size_bytes=self._size_bytes,
            leading_bytes=bytes(self._sample),
            max_size_bytes=self._max_size_bytes,
        )
        return ImportUploadSummary(
            file_name=inspected.file_name,
            file_type=inspected.file_type,
            size_bytes=inspected.size_bytes,
            sha256=self._hash.hexdigest(),
            leading_bytes=bytes(self._sample),
        )


def _starts_with(value: bytes, prefix: bytes) -> bool:
    """判断样本是否以给定二进制签名开始。"""
    return len(value) >= len(prefix) and value[: len(prefix)] == prefix


def _validation_error(message: str) -> ApplicationError:
    """建立统一的上传内容校验错误。"""
    return ApplicationError(
        code="IMPORT_VALIDATION_FAILED",
        message=message,
        status_code=422,
        retryable=False,
    )

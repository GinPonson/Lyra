"""从上传字节生成导入预览。

把「上传边界校验 → CSV 解析 → 预览生成」串成一步，供路由层调用。

**一处明确的未实现**：``.xlsx`` 目前会得到一个显式的
``IMPORT_FORMAT_NOT_IMPLEMENTED``（501），而不是伪装成格式不支持或静默失败。
TypeScript 侧有 ``schwab-xlsx.ts``，Python 侧尚未移植 Excel 解析——这是缺口，
不是设计选择。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain.ledger.batch import TransactionBatchInput
from ..infrastructure.imports.schwab_csv import parse_schwab_csv
from ..infrastructure.imports.schwab_xlsx import parse_schwab_xlsx
from .import_preview import (
    CreateImportPreviewInput,
    ImportPreviewResult,
    create_import_preview,
)
from .import_upload import DEFAULT_SAMPLE_SIZE_BYTES, ImportUploadMonitor

# 单次导入的文件大小上限：512 MiB。券商导出远超此值的文件大概率是导错了。
MAX_IMPORT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ParsedUpload:
    """已通过边界校验并完成解析的上传。"""

    file_name: str
    file_hash: str
    file_type: str
    size_bytes: int
    batch: TransactionBatchInput


@dataclass(frozen=True)
class PreparedImportPreview:
    """上传字节经过校验与解析后的完整预览材料。"""

    file_name: str
    file_hash: str
    file_type: str
    size_bytes: int
    batch: TransactionBatchInput
    preview: ImportPreviewResult


def prepare_import_preview(
    *,
    file_name: str,
    content: bytes,
    parser_version: str,
    base_ledger_version: int,
    existing_transaction_hashes: frozenset[str],
    now: datetime,
    max_size_bytes: int = MAX_IMPORT_BYTES,
    sample_size_bytes: int = DEFAULT_SAMPLE_SIZE_BYTES,
) -> PreparedImportPreview:
    """校验上传字节并生成导入预览。

    @param file_name: 客户端提供的原始文件名。
    @param content: 文件全部字节。
    @param parser_version: 标准化规则版本；参与预览指纹。
    @param base_ledger_version: 预览时观察到的账本版本；空账本为 0。
    @param existing_transaction_hashes: 目标账户已有的交易哈希。
    @param now: 服务端可信时间。
    @param max_size_bytes: 文件大小上限。
    @param sample_size_bytes: 头部采样上限，用于签名判定。
    @returns 校验后的元数据、解析批次与预览。
    @raises ApplicationError: 上传不合法（422/415），或格式尚未实现（501）。
    @raises IngestError: CSV 内容不符合标准化规则。
    """
    parsed = parse_uploaded_file(
        file_name=file_name,
        content=content,
        max_size_bytes=max_size_bytes,
        sample_size_bytes=sample_size_bytes,
    )
    preview = create_import_preview(
        CreateImportPreviewInput(
            batch=parsed.batch,
            parser_version=parser_version,
            base_ledger_version=base_ledger_version,
            existing_transaction_hashes=existing_transaction_hashes,
        )
    )
    return PreparedImportPreview(
        file_name=parsed.file_name,
        file_hash=parsed.file_hash,
        file_type=parsed.file_type,
        size_bytes=parsed.size_bytes,
        batch=parsed.batch,
        preview=preview,
    )


def parse_uploaded_file(
    *,
    file_name: str,
    content: bytes,
    max_size_bytes: int = MAX_IMPORT_BYTES,
    sample_size_bytes: int = DEFAULT_SAMPLE_SIZE_BYTES,
) -> ParsedUpload:
    """校验上传边界并解析为待标准化的批次。

    **与预览生成分开**是因为账户只有解析后才知道（账户标识来自文件名），而预览需要
    该账户的账本上下文（基准版本与已有哈希）。两者顺序不能颠倒。

    @param file_name: 客户端提供的原始文件名。
    @param content: 文件全部字节。
    @param max_size_bytes: 文件大小上限。
    @param sample_size_bytes: 头部采样上限，用于签名判定。
    @returns 校验后的元数据与解析批次。
    @raises ApplicationError: 上传不合法（422/415），或格式尚未实现（501）。
    @raises IngestError: 内容不符合 CSV 或文件名规则。
    """
    monitor = ImportUploadMonitor(file_name, max_size_bytes, sample_size_bytes)
    # 逐块投喂：超限会在接收时就抛错，而不是把整个文件读进内存后才发现。
    for offset in range(0, len(content), 64 * 1024):
        monitor.feed(content[offset : offset + 64 * 1024])
    monitor.finish()
    summary = monitor.summary()

    # 按容器分发到各自解析器；两者共用 `schwab_table` 的表头与列序规则，
    # 因此同一份数据无论来自 CSV 还是 XLSX，产出的领域字段完全一致。
    if summary.file_type == "xlsx":
        batch = parse_schwab_xlsx(
            content,
            file_name=summary.file_name,
            source_hash=summary.sha256,
            source_reference=summary.file_name,
        )
    else:
        batch = parse_schwab_csv(
            content,
            file_name=summary.file_name,
            source_hash=summary.sha256,
            source_reference=summary.file_name,
        )
    return ParsedUpload(
        file_name=summary.file_name,
        file_hash=summary.sha256,
        file_type=summary.file_type,
        size_bytes=summary.size_bytes,
        batch=batch,
    )

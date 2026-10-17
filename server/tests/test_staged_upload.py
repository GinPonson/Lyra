"""上传暂存测试。

暂存是预览与提交之间的桥：解析结果不持久化，因此提交时从暂存读回原始字节并
**校验哈希**。三条性质各有专门用例：

1. **路径只用作业 UUID** —— 否则客户端文件名里的 `..` 能把文件写到目录之外；
2. **读回校验哈希** —— 窗口期内字节被替换或损坏必须拒绝，而不是照常提交；
3. **原子写入** —— 崩溃不会留下可被当成完整上传的半截文件。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lyra.infrastructure.imports.staged_upload import StagedUploadStore

JOB_ID = "44444444-4444-4444-4444-444444444444"
CONTENT = b"Date,Action\n01/05/2026,Buy\n"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture()
def store(tmp_path: Path) -> StagedUploadStore:
    return StagedUploadStore(str(tmp_path / "staging"))


def test_stage_then_load_round_trips(store: StagedUploadStore) -> None:
    """暂存后读回必须逐字节一致。"""
    store.stage(JOB_ID, CONTENT, DIGEST)

    assert store.exists(JOB_ID) is True
    assert store.load(JOB_ID, DIGEST) == CONTENT


def test_stage_creates_the_directory(tmp_path: Path) -> None:
    """暂存目录不存在时自动创建。"""
    target = tmp_path / "deep" / "nested" / "staging"
    StagedUploadStore(str(target)).stage(JOB_ID, CONTENT, DIGEST)

    assert target.is_dir()


def test_stage_rejects_bytes_that_do_not_match_the_hash(store: StagedUploadStore) -> None:
    """写入前就发现字节与声明哈希不符时必须拒绝，不落一个坏文件。"""
    with pytest.raises(ValueError, match="哈希不符"):
        store.stage(JOB_ID, CONTENT, "0" * 64)

    assert store.exists(JOB_ID) is False


def test_load_rejects_content_replaced_after_preview(store: StagedUploadStore) -> None:
    """**窗口期内被替换的字节必须被拒绝**，而不是照常提交。"""
    store.stage(JOB_ID, CONTENT, DIGEST)
    # 模拟暂存文件在预览之后被改动。
    path = store._path_for(JOB_ID)
    path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="与预览时不一致"):
        store.load(JOB_ID, DIGEST)


def test_load_missing_file_raises(store: StagedUploadStore) -> None:
    """暂存文件不存在时抛 FileNotFoundError（已被提交或清理）。"""
    with pytest.raises(FileNotFoundError):
        store.load(JOB_ID, DIGEST)


def test_discard_is_idempotent(store: StagedUploadStore) -> None:
    """删除不存在的内容不报错。"""
    store.stage(JOB_ID, CONTENT, DIGEST)
    store.discard(JOB_ID)
    store.discard(JOB_ID)

    assert store.exists(JOB_ID) is False


@pytest.mark.parametrize(
    "import_id",
    [
        "",
        "not-a-uuid",
        "../../etc/passwd",
        "../escape",
        "44444444-4444-4444-4444-44444444444",  # 少一位
        "44444444-4444-4444-4444-4444444444440",  # 多一位
    ],
)
def test_path_never_accepts_non_uuid(store: StagedUploadStore, import_id: str) -> None:
    """**路径只能由作业 UUID 构成**，客户端不能靠 ID 影响落盘位置。"""
    for operation in (
        lambda: store.stage(import_id, CONTENT, DIGEST),
        lambda: store.load(import_id, DIGEST),
        lambda: store.discard(import_id),
        lambda: store.exists(import_id),
    ):
        with pytest.raises(ValueError, match="UUID"):
            operation()


def test_staged_file_never_uses_the_client_file_name(tmp_path: Path) -> None:
    """暂存文件名与客户端提供的文件名无关。"""
    store = StagedUploadStore(str(tmp_path / "staging"))
    store.stage(JOB_ID, CONTENT, DIGEST)

    entries = [entry.name for entry in Path(tmp_path / "staging").iterdir()]
    assert entries == [f"{JOB_ID}.upload"]


def test_no_leftover_temporary_files(store: StagedUploadStore, tmp_path: Path) -> None:
    """原子写入完成后不得留下临时文件。"""
    store.stage(JOB_ID, CONTENT, DIGEST)

    leftovers = [p.name for p in Path(store._root).iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_empty_staging_dir_is_rejected() -> None:
    """空暂存目录配置是服务端配置错误。"""
    with pytest.raises(ValueError, match="不能为空"):
        StagedUploadStore("   ")

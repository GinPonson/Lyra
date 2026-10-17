"""导入上传的暂存。

提交阶段需要重新解析上传内容，而解析结果**不持久化**（只存指纹与统计）。因此
预览时必须把原始字节落到暂存目录，提交时读回并校验哈希——这也是
``LYRA_IMPORT_STAGING_DIR`` 这个配置存在的原因。

三条安全性质：

1. **路径只用作业 UUID 拼**，绝不使用客户端提供的文件名——否则文件名里的
   ``..`` 或分隔符会把文件写到暂存目录之外；
2. **读回时校验哈希**。预览与提交之间存在时间窗，字节必须与预览时逐位一致；
   不一致说明文件被替换或损坏，必须拒绝而不是照常提交；
3. **原子写入**（先写临时文件再改名），避免半截文件被当成完整上传读走。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SUFFIX = ".upload"


class StagedUploadStore:
    """按作业 ID 暂存上传字节。

    @param staging_dir: 暂存目录；不存在时会自动创建。
    """

    def __init__(self, staging_dir: str) -> None:
        if not staging_dir.strip():
            raise ValueError("导入暂存目录不能为空")
        self._root = Path(staging_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    def stage(self, import_id: str, content: bytes, expected_hash: str) -> Path:
        """把上传字节原子写入暂存目录。

        @param import_id: 作业 ID（必须是 UUID）。
        @param content: 文件全部字节。
        @param expected_hash: 预览时算出的 SHA-256，写入前先自校验。
        @returns 暂存文件路径。
        @raises ValueError: 作业 ID 不是 UUID，或字节与声明哈希不符。
        """
        target = self._path_for(import_id)
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_hash:
            # 写入前就发现不符：宁可拒绝，也不要落一个"看起来正常"的坏文件。
            raise ValueError("上传字节与声明的哈希不符")
        # 先写临时文件再改名：崩溃时只会留下临时文件，不会留下半截的正式文件。
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
        return target

    def load(self, import_id: str, expected_hash: str) -> bytes:
        """读回暂存字节并校验哈希。

        @param import_id: 作业 ID。
        @param expected_hash: 预览时记录的 SHA-256。
        @returns 文件字节。
        @raises FileNotFoundError: 暂存文件不存在（已被提交或清理）。
        @raises ValueError: 字节与预览时不一致。
        """
        content = self._path_for(import_id).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError("暂存的上传内容与预览时不一致，必须重新预览")
        return content

    def discard(self, import_id: str) -> None:
        """删除暂存文件；不存在时静默返回（幂等）。"""
        self._path_for(import_id).unlink(missing_ok=True)

    def exists(self, import_id: str) -> bool:
        """暂存文件是否仍在。"""
        return self._path_for(import_id).exists()

    def _path_for(self, import_id: str) -> Path:
        """按作业 ID 生成暂存路径。

        **绝不用客户端文件名拼路径**；作业 ID 先按 UUID 校验，再拼接。

        @raises ValueError: 作业 ID 不是 UUID。
        """
        if not _UUID.fullmatch(import_id):
            raise ValueError("导入暂存路径只能由作业 UUID 构成")
        return self._root / f"{import_id.lower()}{_SUFFIX}"

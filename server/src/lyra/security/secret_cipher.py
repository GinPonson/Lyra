"""AES-256-GCM 秘密加密，与 TypeScript 版 ``secret-cipher.ts`` 字节级互通。

**为什么必须逐字节兼容**：现有账本里已经存在由 TypeScript 服务端加密的 Gmail
refresh token。切换后 Python 若解不开这些密文，全部 Gmail 连接立即失效，
而这不是「重登一次」就能恢复的——需要用户对每个连接重新授权。

信封格式（与 TypeScript 完全相同）::

    v1.<iv_base64url>.<ciphertext_base64url>.<auth_tag_base64url>

- 密钥：`LYRA_SECRET_MASTER_KEY` 的 **hex 解码**结果，恰好 32 字节
  （注意：生产路径是 hex，不是 base64url——`parseSecretEncryptionKey`
  在 TypeScript 侧只被它自己的测试使用）；
- IV：12 字节随机；GCM 认证标签：16 字节；
- AAD：连接上下文的 UTF-8 字节，例如 ``gmail-connection:<uuid>``，
  使密文即使被串换到另一行也无法通过认证；
- base64url **不带填充**。
"""

from __future__ import annotations

import base64
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-GCM 推荐的 96 位随机 nonce 长度。
IV_BYTES = 12
# AES-256-GCM 认证标签长度。
AUTH_TAG_BYTES = 16
# 当前持久化密文格式版本。
ENVELOPE_VERSION = "v1"
# 主密钥必须恰好为 32 字节（AES-256）。
KEY_BYTES = 32

_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]*$")


class SecretDecryptionError(Exception):
    """密文格式、上下文或认证标签无法通过验证。

    错误消息刻意不含密钥、令牌或原始密文，只说明失败类别。
    """


def master_key_from_hex(encoded: str) -> bytes:
    """把部署配置中的主密钥解析为 32 字节。

    @param encoded: 64 位十六进制字符串（``LYRA_SECRET_MASTER_KEY``）。
    @returns 可直接用于 AES-256 的 32 字节密钥。
    @throws ValueError: 长度或字符集不符。
    """
    if not re.fullmatch(r"[0-9a-fA-F]{64}", encoded):
        raise ValueError("秘密加密主密钥必须是 64 位十六进制")
    return bytes.fromhex(encoded)


def _encode_base64url(data: bytes) -> str:
    """编码为不带填充的 base64url；与 Node 的 ``toString('base64url')`` 同形。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str, label: str) -> bytes:
    """严格解码信封中的 base64url 段。

    @param value: 待解码文本。
    @param label: 出错时用于定位的字段名。
    @returns 解码后的字节。
    @throws SecretDecryptionError: 字符集非法或长度不可能（``len % 4 == 1``）。
    """
    if not _BASE64URL_PATTERN.fullmatch(value) or len(value) % 4 == 1:
        raise SecretDecryptionError(f"加密秘密的 {label} 不是有效 Base64URL")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SecretCipher:
    """加密 Gmail refresh token 等仅服务端可读的持久化秘密。"""

    def __init__(self, key: bytes) -> None:
        """@param key: 恰好 32 字节的 AES-256 主密钥。@throws ValueError: 长度不符。"""
        if len(key) != KEY_BYTES:
            raise ValueError("AES-256 主密钥必须恰好为 32 字节")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, context: str) -> str:
        """加密一个秘密。

        @param plaintext: 待加密文本。
        @param context: 稳定且不敏感的 AAD，例如 ``gmail-connection:<uuid>``。
        @returns 可直接存入 VARCHAR 的版本化密文信封。
        @throws ValueError: 明文为空或上下文为空白。
        """
        if not plaintext or not context.strip():
            raise ValueError("待加密秘密和加密上下文不能为空")
        iv = os.urandom(IV_BYTES)
        # AESGCM.encrypt 返回 ciphertext||tag，信封需要把它们分开存放。
        blob = self._aead.encrypt(iv, plaintext.encode("utf-8"), context.encode("utf-8"))
        ciphertext, auth_tag = blob[:-AUTH_TAG_BYTES], blob[-AUTH_TAG_BYTES:]
        return ".".join((
            ENVELOPE_VERSION,
            _encode_base64url(iv),
            _encode_base64url(ciphertext),
            _encode_base64url(auth_tag),
        ))

    def decrypt(self, envelope: str, context: str) -> str:
        """解密并认证一个信封。

        @param envelope: :meth:`encrypt` 生成的版本化密文。
        @param context: 必须与加密时完全一致的连接上下文。
        @returns 通过 GCM 认证后的 UTF-8 明文。
        @throws SecretDecryptionError: 格式、版本、长度、上下文或认证标签无效。
        """
        parts = envelope.split(".")
        if len(parts) != 4 or parts[0] != ENVELOPE_VERSION or not context.strip():
            raise SecretDecryptionError("加密秘密的信封格式或版本无效")

        iv = _decode_base64url(parts[1], "IV")
        ciphertext = _decode_base64url(parts[2], "ciphertext")
        auth_tag = _decode_base64url(parts[3], "auth tag")
        if len(iv) != IV_BYTES or len(auth_tag) != AUTH_TAG_BYTES:
            raise SecretDecryptionError("加密秘密的 IV 或认证标签长度无效")

        try:
            plaintext = self._aead.decrypt(iv, ciphertext + auth_tag, context.encode("utf-8"))
        except InvalidTag as error:
            raise SecretDecryptionError("加密秘密认证失败") from error
        # 密文解密成功但内容不是 UTF-8 时同样是损坏的秘密，必须显式失败。
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SecretDecryptionError("加密秘密认证失败") from error

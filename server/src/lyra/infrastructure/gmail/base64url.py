"""Gmail MIME payload Base64URL validation and decoding."""

from __future__ import annotations

import base64
import binascii
import re

_BASE64URL = re.compile(r"[A-Za-z0-9_-]*={0,2}")


def decode_base64url(value: str) -> bytes:
    """严格解码规范的 Gmail Base64URL，接受末尾填充或无填充形式。

    校验字符、填充位置与长度，并通过重新编码拒绝非规范 pad bits；不会接受
    空白、标准 Base64 的 ``+``/``/`` 字符或中间/多余填充。

    @param value: Gmail MIME 部件或附件中的 Base64URL 字符串；空字符串解码为空字节。
    @returns 解码后的字节。
    @raises ValueError: 输入不是规范的 Base64URL 编码。
    """
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        raise ValueError("invalid Base64URL data")

    padding = len(value) - len(value.rstrip("="))
    encoded = value[:-padding] if padding else value
    if padding:
        expected_padding = (-len(encoded)) % 4
        if expected_padding != padding or len(value) % 4 != 0:
            raise ValueError("invalid Base64URL padding")
    elif len(encoded) % 4 == 1:
        raise ValueError("invalid Base64URL length")

    padded = value if padding else encoded + "=" * ((-len(encoded)) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("invalid Base64URL data") from error

    canonical_padded = base64.urlsafe_b64encode(decoded).decode("ascii")
    if value not in (canonical_padded, canonical_padded.rstrip("=")):
        raise ValueError("non-canonical Base64URL data")
    return decoded

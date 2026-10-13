"""AES-256-GCM 秘密加密的跨实现互通测试。

最要紧的一条是 :func:`test_decrypts_typescript_ciphertext`：它用**真实的
TypeScript 密文**（迁移期生成并冻结提交的夹具）
验证 Python 能否解开现有账本里的秘密。其余用例覆盖格式、AAD 与篡改检测。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lyra.security.secret_cipher import (
    AUTH_TAG_BYTES,
    ENVELOPE_VERSION,
    IV_BYTES,
    SecretCipher,
    SecretDecryptionError,
    master_key_from_hex,
)

FIXTURES = Path(__file__).parent / "fixtures" / "crypto-fixtures.json"
# 仅用于测试向量的固定密钥；与任何真实部署无关。
TEST_KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
ENVELOPE_PATTERN = re.compile(r"^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+$")


@pytest.fixture(scope="module")
def cipher() -> SecretCipher:
    """使用固定测试密钥的加密器。"""
    return SecretCipher(master_key_from_hex(TEST_KEY_HEX))


def _load_fixtures() -> dict[str, object]:
    """读取 TypeScript 生成的密文夹具。"""
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def test_typescript_fixture_key_matches_test_key() -> None:
    """夹具里的密钥必须与测试常量一致，否则其余用例失去意义。"""
    assert _load_fixtures()["keyHex"] == TEST_KEY_HEX


def test_decrypts_typescript_ciphertext(cipher: SecretCipher) -> None:
    """Python 必须能解开 TypeScript 产生的全部密文。

    这是切换关键路径：解不开就意味着现有 Gmail 连接在切换后全部失效。
    """
    payload = _load_fixtures()
    assert payload["producedBy"] == "typescript"
    case_list = payload["cases"]
    assert isinstance(case_list, list) and len(case_list) >= 5

    for case in case_list:
        plaintext = cipher.decrypt(case["envelope"], case["context"])
        assert plaintext == case["plaintext"], f"用例 {case['name']} 解出的明文不一致"


def test_decrypts_typescript_ciphertext_with_unicode(cipher: SecretCipher) -> None:
    """多字节明文与多字节 AAD 都必须逐字节往返。"""
    payload = _load_fixtures()
    case = next(
        case for case in payload["cases"] if case["name"] == "unicode_plaintext"
    )
    assert cipher.decrypt(case["envelope"], case["context"]) == "令牌-测试-🔐"


def test_envelope_shape_matches_typescript(cipher: SecretCipher) -> None:
    """信封必须是四段、无填充的 base64url，与 TypeScript 同形。"""
    envelope = cipher.encrypt("payload", "gmail-connection:shape")

    assert ENVELOPE_PATTERN.fullmatch(envelope), envelope
    assert "=" not in envelope
    assert envelope.split(".")[0] == ENVELOPE_VERSION


def test_round_trip(cipher: SecretCipher) -> None:
    """自行加密后必须能自行解密。"""
    for plaintext, context in (
        ("1//refresh-token", "gmail-connection:a"),
        ("", "gmail-connection:b"),
    ):
        if plaintext == "":
            # 空明文必须被拒绝，而不是产出可解密的空信封。
            with pytest.raises(ValueError, match="不能为空"):
                cipher.encrypt(plaintext, context)
            continue
        assert cipher.decrypt(cipher.encrypt(plaintext, context), context) == plaintext


def test_each_encryption_uses_fresh_iv(cipher: SecretCipher) -> None:
    """相同明文与上下文两次加密必须得到不同密文。"""
    first = cipher.encrypt("same", "gmail-connection:c")
    second = cipher.encrypt("same", "gmail-connection:c")

    assert first != second
    assert first.split(".")[1] != second.split(".")[1]


def _flip_first(segment: str) -> str:
    """翻转 base64url 段的**首个**字符。

    必须改首字符：无填充 base64url 的最后一个字符可能只承载未使用位，
    改动它未必改变解码后的字节，会让篡改测试假通过。
    """
    return ("A" if segment[0] != "A" else "B") + segment[1:]


def test_rejects_tampered_ciphertext(cipher: SecretCipher) -> None:
    """篡改密文必须被 GCM 认证拒绝。"""
    envelope = cipher.encrypt("payload", "gmail-connection:d")
    version, iv, ciphertext, tag = envelope.split(".")
    tampered = ".".join((version, iv, _flip_first(ciphertext), tag))

    with pytest.raises(SecretDecryptionError, match="认证失败"):
        cipher.decrypt(tampered, "gmail-connection:d")


def test_rejects_tampered_auth_tag(cipher: SecretCipher) -> None:
    """篡改认证标签必须被拒绝。"""
    envelope = cipher.encrypt("payload", "gmail-connection:e")
    version, iv, ciphertext, tag = envelope.split(".")

    with pytest.raises(SecretDecryptionError, match="认证失败"):
        cipher.decrypt(".".join((version, iv, ciphertext, _flip_first(tag))), "gmail-connection:e")


def test_rejects_wrong_context(cipher: SecretCipher) -> None:
    """上下文不符必须被拒绝——这保证密文被串换到另一行也解不开。"""
    envelope = cipher.encrypt("payload", "gmail-connection:right")

    with pytest.raises(SecretDecryptionError, match="认证失败"):
        cipher.decrypt(envelope, "gmail-connection:wrong")


def test_rejects_malformed_envelopes(cipher: SecretCipher) -> None:
    """格式、版本、段数与长度不符都必须被拒绝，且不泄漏原始密文。"""
    valid = cipher.encrypt("payload", "gmail-connection:f")
    version, iv, ciphertext, tag = valid.split(".")

    malformed = [
        "",
        "v2.a.b.c",
        "v1.a.b",
        "v1.a.b.c.d",
        f"v1.{iv}.{ciphertext}.{tag}.",
        f"v1.{iv}.{ciphertext}.{tag}.",
        # IV 长度不足（12 字节需要 16 个 base64url 字符）。
        f"v1.AAAA.{ciphertext}.{tag}",
        # 认证标签长度不足。
        f"v1.{iv}.{ciphertext}.AAAA",
        # 非法 base64url 字符。
        f"v1.{iv}!.{ciphertext}.{tag}",
    ]
    for envelope in malformed:
        with pytest.raises(SecretDecryptionError):
            cipher.decrypt(envelope, "gmail-connection:f")


def test_rejects_blank_context(cipher: SecretCipher) -> None:
    """空白上下文不能用于加密或解密。"""
    with pytest.raises(ValueError, match="不能为空"):
        cipher.encrypt("payload", "   ")
    with pytest.raises(SecretDecryptionError, match="格式或版本无效"):
        cipher.decrypt(cipher.encrypt("payload", "gmail-connection:g"), "   ")


def test_key_length_is_enforced() -> None:
    """密钥必须是 32 字节。"""
    with pytest.raises(ValueError, match="32 字节"):
        SecretCipher(b"\x00" * 31)
    with pytest.raises(ValueError, match="32 字节"):
        SecretCipher(b"\x00" * 33)


def test_master_key_hex_parsing() -> None:
    """主密钥必须是 64 位十六进制。"""
    assert len(master_key_from_hex(TEST_KEY_HEX)) == 32
    assert master_key_from_hex(TEST_KEY_HEX.upper()) == master_key_from_hex(TEST_KEY_HEX)
    for invalid in ("", "0" * 63, "0" * 65, "z" * 64, "00" * 32 + "\n"):
        with pytest.raises(ValueError, match="64 位十六进制"):
            master_key_from_hex(invalid)


def test_envelope_segments_have_expected_lengths(cipher: SecretCipher) -> None:
    """IV 与认证标签解码后必须分别是 12 与 16 字节。"""
    import base64

    version, iv, ciphertext, tag = cipher.encrypt("payload", "gmail-connection:h").split(".")
    assert version == ENVELOPE_VERSION

    def decode(segment: str) -> bytes:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))

    assert len(decode(iv)) == IV_BYTES
    assert len(decode(tag)) == AUTH_TAG_BYTES
    assert decode(ciphertext) != b""

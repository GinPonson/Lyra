"""配对挑战与设备公钥规范化测试。

最有分量的一条是 :func:`test_public_key_normalization_matches_typescript`：
用**真实公钥**（RSA / EC / Ed25519）验证 Python 的规范化结果与 TypeScript
逐字节一致。已配对设备记录里存的是规范化后的 SPKI PEM 与 SHA-256 指纹，
两边只要有一点差异（换行、行宽、编码），切换后既有设备记录就会像被改写过。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from lyra.security.device_auth import (
    PAIRING_ALPHABET,
    DeviceAuthError,
    PairingChallengeRecord,
    consume_pairing_challenge,
    create_paired_device_record,
    create_pairing_challenge,
    normalize_device_public_key,
)

FIXTURES = Path(__file__).parent / "fixtures" / "device-keys.json"

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
TTL_MS = 600_000

# schema 16 起设备必须归属一个用户与一条登录身份（``user_id`` / ``identity_id``
# 为 NOT NULL + 复合外键），不再是可选的单 owner 附属信息。
USER_ID = "11111111-1111-1111-1111-111111111111"
IDENTITY_ID = "22222222-2222-2222-2222-222222222222"

GROUPED_CODE = re.compile(r"^[A-Z2-9]{5}-[A-Z2-9]{5}$")


def _challenge() -> tuple[str, PairingChallengeRecord]:
    """新建一枚配对挑战。"""
    created = create_pairing_challenge(NOW, TTL_MS)
    return created.code, created.record


def _fixture_cases() -> list[dict[str, str]]:
    """读取 TypeScript 生成的公钥夹具。"""
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert payload["producedBy"] == "typescript"
    return payload["cases"]


def test_pairing_code_shape() -> None:
    """配对码必须是 5-5 分组，且只含去混淆字母表里的字符。"""
    code, record = _challenge()

    assert GROUPED_CODE.fullmatch(code), code
    for character in code.replace("-", ""):
        assert character in PAIRING_ALPHABET, f"含字母表外字符: {character}"
    assert len(record.code_hash) == 64
    # 明文码不得出现在记录里。
    assert code.replace("-", "") not in record.code_hash


def test_pairing_codes_are_unique() -> None:
    """两次生成的明文码不得相同。"""
    assert _challenge()[0] != _challenge()[0]


@pytest.mark.parametrize("ttl", [0, -1, 1.5, True])
def test_pairing_rejects_invalid_ttl(ttl: object) -> None:
    """有效期必须是正安全整数毫秒数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        create_pairing_challenge(NOW, ttl)  # type: ignore[arg-type]


def test_consume_accepts_grouped_and_lowercase_input() -> None:
    """输入允许大小写差异、显示横线与空格。"""
    code, record = _challenge()
    raw = code.replace("-", "")

    for candidate in (code, raw, code.lower(), f" {code} ", raw[:5] + " " + raw[5:]):
        consumed = consume_pairing_challenge(record, candidate, NOW)
        assert consumed.consumed_at == "2026-10-12T09:00:00.000Z"


def test_consume_does_not_mutate_input_record() -> None:
    """消费返回新记录，原记录保持不变。"""
    code, record = _challenge()
    consumed = consume_pairing_challenge(record, code, NOW)

    assert record.consumed_at is None
    assert consumed is not record


def test_consume_rejects_wrong_code() -> None:
    """配对码错误必须报「无效」。"""
    _, record = _challenge()
    with pytest.raises(DeviceAuthError) as raised:
        consume_pairing_challenge(record, "AAAAA-AAAAA", NOW)
    assert raised.value.code == "PAIRING_CODE_INVALID"


def test_consume_rejects_used_code() -> None:
    """已使用的配对码必须报「已使用」，不能重复消费。"""
    code, record = _challenge()
    consumed = consume_pairing_challenge(record, code, NOW)

    with pytest.raises(DeviceAuthError) as raised:
        consume_pairing_challenge(consumed, code, NOW)
    assert raised.value.code == "PAIRING_CODE_USED"


def test_consume_rejects_expired_code() -> None:
    """到期时刻（含等于）必须判过期。"""
    code, record = _challenge()
    at_expiry = NOW + timedelta(milliseconds=TTL_MS)

    with pytest.raises(DeviceAuthError) as raised:
        consume_pairing_challenge(record, code, at_expiry)
    assert raised.value.code == "PAIRING_CODE_EXPIRED"


def test_consume_check_order_hides_used_state_from_wrong_code() -> None:
    """配错码时不得泄漏「该码已被使用」这一状态。"""
    code, record = _challenge()
    consumed = consume_pairing_challenge(record, code, NOW)
    wrong = "BBBBB-BBBBB" if code != "BBBBB-BBBBB" else "CCCCC-CCCCC"

    with pytest.raises(DeviceAuthError) as raised:
        consume_pairing_challenge(consumed, wrong, NOW)
    assert raised.value.code == "PAIRING_CODE_INVALID"


def test_public_key_normalization_matches_typescript() -> None:
    """RSA / EC / Ed25519 三种公钥的规范化结果必须与 TypeScript 完全一致。"""
    cases = _fixture_cases()
    assert len(cases) >= 3

    for case in cases:
        normalized = normalize_device_public_key(case["inputPem"])
        assert normalized.pem == case["normalizedPem"], f"{case['name']}: PEM 不一致"
        assert normalized.fingerprint == case["fingerprint"], f"{case['name']}: 指纹不一致"
        assert normalized.algorithm == case["algorithm"], f"{case['name']}: 算法名不一致"


def test_public_key_normalization_is_idempotent() -> None:
    """规范化结果再次规范化必须保持不变。"""
    case = _fixture_cases()[0]
    once = normalize_device_public_key(case["inputPem"])
    twice = normalize_device_public_key(once.pem)

    assert twice.pem == once.pem
    assert twice.fingerprint == once.fingerprint


def test_public_key_rejects_private_key() -> None:
    """私钥必须被明确拒绝，且错误信息点明原因。"""
    private_pem = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    ).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    with pytest.raises(DeviceAuthError) as raised:
        normalize_device_public_key(private_pem)
    assert raised.value.code == "DEVICE_PUBLIC_KEY_INVALID"
    assert "私钥" in raised.value.message


@pytest.mark.parametrize("pem", ["", "not a key", "-----BEGIN PUBLIC KEY-----\nzzz\n-----END PUBLIC KEY-----\n"])
def test_public_key_rejects_malformed_input(pem: str) -> None:
    """非公钥文本必须报错而不是静默接受。"""
    with pytest.raises(DeviceAuthError) as raised:
        normalize_device_public_key(pem)
    assert raised.value.code == "DEVICE_PUBLIC_KEY_INVALID"


def test_create_paired_device_record() -> None:
    """设备记录必须保存规范化公钥、指纹与归属用户/身份，且初始为 active。"""
    case = _fixture_cases()[0]
    public_key = normalize_device_public_key(case["inputPem"])

    record = create_paired_device_record(
        "device-1", " Pixel 9 ", public_key, NOW,
        user_id=USER_ID, identity_id=IDENTITY_ID,
    )

    assert record.id == "device-1"
    assert record.user_id == USER_ID
    assert record.identity_id == IDENTITY_ID
    assert record.display_name == "Pixel 9"
    assert record.public_key_pem == case["normalizedPem"]
    assert record.public_key_fingerprint == case["fingerprint"]
    assert record.key_algorithm == case["algorithm"]
    assert record.status == "active"
    assert record.paired_at == "2026-10-12T09:00:00.000Z"
    assert record.last_seen_at is None
    assert record.revoked_at is None


@pytest.mark.parametrize(("device_id", "name"), [("", "Phone"), ("   ", "Phone"), ("device-1", "  ")])
def test_create_paired_device_rejects_blank_text(device_id: str, name: str) -> None:
    """设备 ID 与名称都不能为空白。"""
    public_key = normalize_device_public_key(_fixture_cases()[0]["inputPem"])
    with pytest.raises(ValueError, match="不能为空"):
        create_paired_device_record(
            device_id, name, public_key, NOW,
            user_id=USER_ID, identity_id=IDENTITY_ID,
        )


@pytest.mark.parametrize(
    ("user_id", "identity_id"),
    [("", IDENTITY_ID), ("   ", IDENTITY_ID), (USER_ID, ""), (USER_ID, "  ")],
)
def test_create_paired_device_rejects_blank_ownership(user_id: str, identity_id: str) -> None:
    """归属用户与登录身份都不能为空白——没有归属的设备是不可表示的状态。"""
    public_key = normalize_device_public_key(_fixture_cases()[0]["inputPem"])
    with pytest.raises(ValueError, match="不能为空"):
        create_paired_device_record(
            "device-1", "Phone", public_key, NOW,
            user_id=user_id, identity_id=identity_id,
        )


def test_ec_and_rsa_keys_are_distinguishable() -> None:
    """不同算法的指纹必须不同，避免把两种密钥误判为同一设备。"""
    cases = {case["name"]: case for case in _fixture_cases()}
    assert cases["rsa"]["fingerprint"] != cases["ec"]["fingerprint"]
    assert normalize_device_public_key(cases["ec"]["inputPem"]).algorithm == "ec"
    assert normalize_device_public_key(cases["rsa"]["inputPem"]).algorithm == "rsa"


def test_ec_other_curve_is_still_recognized_as_ec() -> None:
    """非默认曲线的 EC 公钥也必须识别为 ec，而不是回落到 unknown。"""
    key = ec.generate_private_key(ec.SECP521R1()).public_key()
    pem = key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")

    normalized = normalize_device_public_key(pem)

    assert normalized.algorithm == "ec"
    # 不同曲线的指纹必须不同。
    assert normalized.fingerprint != _fixture_cases()[1]["fingerprint"]

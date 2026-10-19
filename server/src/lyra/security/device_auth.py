"""设备凭据与访问令牌。

镜像 TypeScript 版 ``device-auth.ts`` 中与**认证**有关的核心部分。

**安全约束（不可放宽）**：服务端只持久化 ``token_hash``，明文令牌只在签发响应
中返回一次。校验使用常量时间比较，避免通过响应耗时逐字节试探令牌。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import (
    dsa,
    ec,
    ed448,
    ed25519,
    rsa,
    x448,
    x25519,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

DeviceAuthErrorCode = Literal[
    "PAIRING_CODE_INVALID",
    "PAIRING_CODE_EXPIRED",
    "PAIRING_CODE_USED",
    "DEVICE_PUBLIC_KEY_INVALID",
    "DEVICE_CREDENTIAL_INVALID",
    "DEVICE_CREDENTIAL_EXPIRED",
    "DEVICE_CREDENTIAL_REUSED",
    "DEVICE_REVOKED",
]

# 凭据长度：256 位随机值。
_CREDENTIAL_BYTES = 32
# 持久化哈希必须恰好是 64 位小写十六进制。
_TOKEN_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")

# 去混淆 Base32 字母表（去掉 I/L/O/0/1）。
PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# 10 个 Base32 字符约提供 50 位随机性，并保持手工输入可接受。
PAIRING_CODE_CHARACTERS = 10
# 设备活动写入的最小间隔：避免每次认证都产生一次数据库写入。
DEFAULT_DEVICE_ACTIVITY_UPDATE_INTERVAL_MS = 5 * 60 * 1000


class DeviceAuthError(Exception):
    """配对或设备凭据验证失败。

    ``code`` 是 App 可以稳定分支处理的值；``message`` 不含配对码、令牌或私钥。
    """

    def __init__(self, code: DeviceAuthErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreatedOpaqueCredential:
    """新建的不透明凭据：明文令牌与可持久化哈希。"""

    token: str
    token_hash: str


@dataclass(frozen=True)
class PairedDeviceAuthRecord:
    """刷新凭据校验所需的最小设备安全状态。"""

    id: str
    status: Literal["active", "revoked"]
    revoked_at: str | None


@dataclass(frozen=True)
class DeviceAccessTokenRecord:
    """与 DuckDB v6 schema 对齐的短期访问令牌状态。"""

    id: str
    device_id: str
    token_hash: str
    status: Literal["active", "revoked"]
    expires_at: str
    last_seen_at: str | None
    revoked_at: str | None
    issued_at: str


@dataclass(frozen=True)
class IssuedDeviceAccessToken:
    """新签发的访问令牌：明文只应返回一次。"""

    token: str
    record: DeviceAccessTokenRecord


@dataclass(frozen=True)
class DeviceRefreshCredentialRecord:
    """与 DuckDB v3 schema 对齐的设备刷新凭据状态。"""

    id: str
    device_id: str
    family_id: str
    generation: int
    token_hash: str
    status: Literal["active", "rotated", "revoked"]
    expires_at: str
    last_used_at: str | None
    rotated_at: str | None
    revoked_at: str | None
    created_at: str


@dataclass(frozen=True)
class IssuedDeviceRefreshCredential:
    """首次签发的明文刷新凭据及其可持久化记录。"""

    token: str
    record: DeviceRefreshCredentialRecord


@dataclass(frozen=True)
class RotatedDeviceRefreshCredential:
    """刷新成功后必须在同一数据库事务中应用的轮换结果。"""

    token: str
    previous: DeviceRefreshCredentialRecord
    next: DeviceRefreshCredentialRecord


@dataclass(frozen=True)
class RefreshCredentialFamilyRevocationPlan:
    """同一凭据族的批量撤销计划。"""

    device_id: str
    family_id: str
    credentials: tuple[DeviceRefreshCredentialRecord, ...]


@dataclass(frozen=True)
class PairingChallengeRecord:
    """短期存储中的配对挑战，不含明文配对码。"""

    code_hash: str
    expires_at: str
    consumed_at: str | None


@dataclass(frozen=True)
class CreatedPairingChallenge:
    """新建配对挑战时唯一一次返回给服务端操作者的明文码。"""

    code: str
    record: PairingChallengeRecord


@dataclass(frozen=True)
class NormalizedDevicePublicKey:
    """已规范化的设备公钥及其稳定指纹。"""

    pem: str
    fingerprint: str
    algorithm: str


@dataclass(frozen=True)
class PairedDeviceRecord:
    """与 paired_devices 表对齐且不含设备私钥的完整记录。

    ``user_id`` 与 ``identity_id`` 是 schema 16 起 paired_devices 的必填列：设备
    **一旦归属用户就不再改 owner**，且必须记住本次登录用的是哪一条身份，供
    ``GET /v1/me`` 直接取用（P0 契约 §3.7）。
    """

    id: str
    user_id: str
    identity_id: str
    display_name: str
    public_key_pem: str
    public_key_fingerprint: str
    key_algorithm: str
    status: Literal["active", "revoked"]
    paired_at: str
    last_seen_at: str | None
    revoked_at: str | None
    updated_at: str


def create_opaque_credential() -> CreatedOpaqueCredential:
    """生成适合访问令牌或刷新凭据的 256 位随机不透明值。

    @returns 明文令牌与它的 SHA-256 哈希；明文不得持久化。
    """
    token = base64.urlsafe_b64encode(os.urandom(_CREDENTIAL_BYTES)).rstrip(b"=").decode("ascii")
    return CreatedOpaqueCredential(token=token, token_hash=hash_opaque_credential(token))


def hash_opaque_credential(token: str) -> str:
    """对高熵随机凭据生成可持久化的 SHA-256 哈希。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_opaque_credential(token: str, expected_hash: str) -> bool:
    """使用常量时间比较验证不透明凭据。

    @param token: App 提交的明文令牌。
    @param expected_hash: 持久化的期望哈希。
    @returns 是否匹配；格式非法的期望哈希一律视为不匹配。
    """
    if not _TOKEN_HASH_PATTERN.fullmatch(expected_hash):
        return False
    return hmac.compare_digest(hash_opaque_credential(token), expected_hash)


def issue_device_access_token(
    device: PairedDeviceAuthRecord,
    now: datetime,
    ttl_milliseconds: int,
) -> IssuedDeviceAccessToken:
    """为已配对设备签发短期访问令牌。

    @param device: 设备的当前安全状态。
    @param now: 服务端可信时间。
    @param ttl_milliseconds: 有效期毫秒数，必须是正整数。
    @returns 明文令牌与仅含哈希的持久化记录。
    @raises ValueError: 有效期不是正整数。
    @raises DeviceAuthError: 设备已撤销。
    """
    _assert_credential_ttl(ttl_milliseconds)
    if device.status == "revoked":
        raise DeviceAuthError("DEVICE_REVOKED", "设备已经撤销")
    credential = create_opaque_credential()
    return IssuedDeviceAccessToken(
        token=credential.token,
        record=DeviceAccessTokenRecord(
            id=str(uuid.uuid4()),
            device_id=device.id,
            token_hash=credential.token_hash,
            status="active",
            expires_at=_iso(now + timedelta(milliseconds=ttl_milliseconds)),
            last_seen_at=None,
            revoked_at=None,
            issued_at=_iso(now),
        ),
    )


def authenticate_device_access_token(
    device: PairedDeviceAuthRecord,
    record: DeviceAccessTokenRecord,
    token: str,
    now: datetime,
) -> DeviceAccessTokenRecord:
    """校验短期访问令牌并返回更新 ``last_seen_at`` 后的新记录。

    **检查顺序是安全语义的一部分**：先验证凭据本身，再依次判断设备撤销、
    令牌撤销与过期。把顺序调换会改变客户端看到的错误码，进而改变 App 的
    分支行为（例如把「设备已撤销」误报成「令牌无效」）。

    @param device: 令牌关联设备的当前状态。
    @param record: 按提交令牌哈希查得的访问令牌记录。
    @param token: App 提交的 Bearer 令牌明文。
    @param now: 认证发生时间。
    @returns 更新 ``last_seen_at`` 后的记录。
    @raises DeviceAuthError: 凭据无效、设备或令牌已撤销、令牌已过期。
    """
    if record.device_id != device.id or not verify_opaque_credential(token, record.token_hash):
        raise DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备访问令牌无效")
    if device.status == "revoked":
        raise DeviceAuthError("DEVICE_REVOKED", "设备已经撤销")
    if record.status == "revoked":
        raise DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备访问令牌无效")
    if _milliseconds(now) >= _milliseconds(_parse_iso(record.expires_at)):
        raise DeviceAuthError("DEVICE_CREDENTIAL_EXPIRED", "设备访问令牌已经过期")
    return replace(record, last_seen_at=_iso(now))


def create_pairing_challenge(
    now: datetime,
    ttl_milliseconds: int,
) -> CreatedPairingChallenge:
    """新建一次性配对挑战。

    @param now: 服务端可信时钟。
    @param ttl_milliseconds: 配对码有效期毫秒数，必须是正整数。
    @returns 显示用分组码与仅含 SHA-256 哈希的记录。
    @raises ValueError: 有效期不是正整数。
    """
    if (
        not isinstance(ttl_milliseconds, int)
        or isinstance(ttl_milliseconds, bool)
        or ttl_milliseconds <= 0
    ):
        raise ValueError("配对码有效期必须是正安全整数毫秒数")
    raw_code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_CHARACTERS))
    return CreatedPairingChallenge(
        code=f"{raw_code[:5]}-{raw_code[5:]}",
        record=PairingChallengeRecord(
            code_hash=_hash_pairing_code(raw_code),
            expires_at=_iso(now + timedelta(milliseconds=ttl_milliseconds)),
            consumed_at=None,
        ),
    )


def consume_pairing_challenge(
    record: PairingChallengeRecord,
    candidate: str,
    now: datetime,
) -> PairingChallengeRecord:
    """在原子更新事务中校验并消费一次性配对码。

    **检查顺序**：配对码比对 → 已使用 → 已过期。先比对是因为「已使用」本身
    是敏感状态，不应向配错码的一方泄漏。

    @param record: 当前未修改的挑战记录。
    @param candidate: App 提交的配对码，允许包含显示分隔符、空格与大小写差异。
    @param now: 校验时间。
    @returns ``consumed_at`` 已设置的新记录；原记录保持不变。
    @raises DeviceAuthError: 配对码错误、已使用或已过期。
    """
    if not _TOKEN_HASH_PATTERN.fullmatch(record.code_hash):
        raise DeviceAuthError("PAIRING_CODE_INVALID", "配对码无效")
    if not hmac.compare_digest(
        _hash_pairing_code(candidate), record.code_hash
    ):
        raise DeviceAuthError("PAIRING_CODE_INVALID", "配对码无效")
    if record.consumed_at is not None:
        raise DeviceAuthError("PAIRING_CODE_USED", "配对码已经使用")
    if _milliseconds(now) >= _milliseconds(_parse_iso(record.expires_at)):
        raise DeviceAuthError("PAIRING_CODE_EXPIRED", "配对码已经过期")
    return replace(record, consumed_at=_iso(now))


def normalize_device_public_key(pem: str) -> NormalizedDevicePublicKey:
    """把设备提交的公钥规范化并计算稳定指纹。

    只接受 ``PUBLIC KEY`` PEM；**明确拒绝私钥**——让私钥有机会进入服务端
    本身就是不可接受的，无论后续是否持久化。

    @param pem: ``PUBLIC KEY`` PEM 文本。
    @returns 规范 SPKI PEM、SHA-256 指纹与算法名。
    @raises DeviceAuthError: 输入是私钥或不是可识别的公钥。
    """
    if "PRIVATE KEY" in pem:
        raise DeviceAuthError("DEVICE_PUBLIC_KEY_INVALID", "设备只能提交公钥，不能提交私钥")
    try:
        public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception as error:
        raise DeviceAuthError("DEVICE_PUBLIC_KEY_INVALID", "设备公钥格式无效") from error

    der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    normalized_pem = public_key.public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    return NormalizedDevicePublicKey(
        pem=normalized_pem,
        fingerprint=hashlib.sha256(der).hexdigest(),
        algorithm=_asymmetric_key_type(public_key),
    )


def create_paired_device_record(
    device_id: str,
    display_name: str,
    public_key: NormalizedDevicePublicKey,
    now: datetime,
    *,
    user_id: str,
    identity_id: str,
) -> PairedDeviceRecord:
    """在登录身份已解析后创建可持久化的活动设备记录。

    只接受 :func:`normalize_device_public_key` 的结果，因此记录保存的是规范
    SPKI 公钥、稳定指纹与算法名，不接触设备私钥。

    ``user_id`` 与 ``identity_id`` 是**必填**而非可选：设备没有归属用户是不可表示的
    状态（schema 16 的 ``NOT NULL`` + 复合外键），留一个默认为空的字段只会让漏传
    变成运行时才发现的越权。

    @param device_id: 服务端生成的不可变设备 UUID。
    @param display_name: 用户可识别的设备名称。
    @param public_key: 已规范化的设备公钥。
    @param now: 配对事务提交时间。
    @param user_id: 设备的归属用户 UUID；此后不再变更。
    @param identity_id: 本次登录使用的身份 UUID，必须与该用户一致。
    @returns 可持久化的设备记录。
    @raises ValueError: 设备 ID、名称、归属用户或身份为空白。
    """
    timestamp = _iso(now)
    return PairedDeviceRecord(
        id=_require_device_text(device_id, "设备 ID"),
        user_id=_require_device_text(user_id, "归属用户"),
        identity_id=_require_device_text(identity_id, "登录身份"),
        display_name=_require_device_text(display_name, "设备名称"),
        public_key_pem=public_key.pem,
        public_key_fingerprint=public_key.fingerprint,
        key_algorithm=public_key.algorithm,
        status="active",
        paired_at=timestamp,
        last_seen_at=None,
        revoked_at=None,
        updated_at=timestamp,
    )


@dataclass(frozen=True)
class DeviceRevocationPlan:
    """设备撤销事务计划：设备本身与其刷新凭据。"""

    device: PairedDeviceAuthRecord
    credentials: tuple[DeviceRefreshCredentialRecord, ...]


def plan_paired_device_activity_update(
    device: PairedDeviceRecord,
    observed_at: datetime,
    minimum_interval_ms: int = DEFAULT_DEVICE_ACTIVITY_UPDATE_INTERVAL_MS,
) -> PairedDeviceRecord:
    """在认证成功后生成低频的设备活动时间更新。

    **无需写入时返回原对象本身**（而非等价副本）：调用方据此判断是否要发起
    数据库写入。若每次认证都产生新对象，节流就失效了。

    三种情况不产生写入：观察时间不晚于已知的最新时间、距上次记录不足最小间隔、
    以及早于配对时间的非法观察（后者直接报错）。

    @param device: 当前完整活动设备记录。
    @param observed_at: 本次成功认证的服务端可信时间。
    @param minimum_interval_ms: 两次设备活动写入的最小间隔。
    @returns 需要持久化的新记录，或无需写入时的原对象。
    @raises DeviceAuthError: 设备已撤销。
    @raises ValueError: 间隔为负或时间早于配对时间。
    """
    if device.status == "revoked":
        raise DeviceAuthError("DEVICE_REVOKED", "设备已经撤销")
    if (
        not isinstance(minimum_interval_ms, int)
        or isinstance(minimum_interval_ms, bool)
        or minimum_interval_ms < 0
    ):
        raise ValueError("设备活动更新间隔必须是非负安全整数毫秒数")

    observed_time = _milliseconds(observed_at)
    paired_time = _milliseconds(_parse_iso(device.paired_at))
    if observed_time < paired_time:
        raise ValueError("设备活动时间不能早于配对时间")

    latest_known_time = max(
        _milliseconds(_parse_iso(device.updated_at)),
        paired_time if device.last_seen_at is None else _milliseconds(_parse_iso(device.last_seen_at)),
    )
    if observed_time <= latest_known_time or (
        device.last_seen_at is not None
        and observed_time - _milliseconds(_parse_iso(device.last_seen_at)) < minimum_interval_ms
    ):
        return device

    timestamp = _iso(observed_at)
    return replace(device, last_seen_at=timestamp, updated_at=timestamp)


def plan_device_access_token_revocation(
    device_id: str,
    records: tuple[DeviceAccessTokenRecord, ...],
    now: datetime,
) -> tuple[DeviceAccessTokenRecord, ...]:
    """设备撤销事务中将其全部活动访问令牌标记为 revoked。

    已撤销的令牌保持原时间戳不变，重复调用幂等。

    @param device_id: 被撤销设备 ID。
    @param records: 数据库按 device_id 查询出的全部访问令牌。
    @param now: 撤销时间。
    @returns 可在同一事务中批量持久化的令牌记录。
    @raises ValueError: 记录中混入其它设备。
    """
    if any(record.device_id != device_id for record in records):
        raise ValueError("访问令牌撤销计划包含其他设备记录")
    timestamp = _iso(now)
    return tuple(
        replace(record, status="revoked", revoked_at=timestamp)
        if record.status == "active"
        else record
        for record in records
    )


def plan_device_revocation(
    device: PairedDeviceAuthRecord,
    credentials: tuple[DeviceRefreshCredentialRecord, ...],
    now: datetime,
) -> DeviceRevocationPlan:
    """生成设备撤销计划，同时撤销该设备仍处于 active 的刷新凭据。

    **重复撤销保留首次撤销时间**：``revoked_at`` 取设备已有的值，因此重放
    撤销请求不会改写审计时间。

    @param device: 要撤销的设备。
    @param credentials: 由数据库按 ``device.id`` 查询得到的全部凭据。
    @param now: 撤销时间。
    @returns 设备与凭据的批量撤销计划。
    @raises ValueError: 凭据中混入其它设备。
    """
    if any(credential.device_id != device.id for credential in credentials):
        raise ValueError("撤销计划包含其他设备的刷新凭据")
    timestamp = device.revoked_at if device.revoked_at is not None else _iso(now)
    return DeviceRevocationPlan(
        device=replace(device, status="revoked", revoked_at=timestamp),
        credentials=tuple(
            replace(credential, status="revoked", revoked_at=timestamp)
            if credential.status == "active"
            else credential
            for credential in credentials
        ),
    )


def _hash_pairing_code(code: str) -> str:
    """统一配对码的大小写、显示横线与空格后再哈希。"""
    normalized = re.sub(r"[-\s]", "", code.upper())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _require_device_text(value: str, label: str) -> str:
    """规范设备持久化所需的短文本字段。"""
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized


def _asymmetric_key_type(public_key: object) -> str:
    """把公钥类型映射为与 Node ``key.asymmetricKeyType`` 同名的算法名。

    名称必须一致：已配对设备记录里的 ``key_algorithm`` 是可查询字段，
    切换后取值变化会让既有的设备记录看起来像被改写过。
    """
    for key_type, name in (
        (rsa.RSAPublicKey, "rsa"),
        (dsa.DSAPublicKey, "dsa"),
        (ec.EllipticCurvePublicKey, "ec"),
        (ed25519.Ed25519PublicKey, "ed25519"),
        (ed448.Ed448PublicKey, "ed448"),
        (x25519.X25519PublicKey, "x25519"),
        (x448.X448PublicKey, "x448"),
    ):
        if isinstance(public_key, key_type):
            return name
    return "unknown"


def issue_device_refresh_credential(
    device_id: str,
    now: datetime,
    ttl_milliseconds: int,
) -> IssuedDeviceRefreshCredential:
    """为已完成配对的设备签发第一代刷新凭据。

    @param device_id: 已持久化的设备 ID。
    @param now: 服务端可信时钟。
    @param ttl_milliseconds: 刷新凭据有效期毫秒数。
    @returns 明文令牌与仅含哈希的持久化记录；明文只应返回一次。
    @raises ValueError: 有效期不是正整数。
    """
    _assert_credential_ttl(ttl_milliseconds)
    credential = create_opaque_credential()
    timestamp = _iso(now)
    return IssuedDeviceRefreshCredential(
        token=credential.token,
        record=DeviceRefreshCredentialRecord(
            id=str(uuid.uuid4()),
            device_id=device_id,
            family_id=str(uuid.uuid4()),
            generation=1,
            token_hash=credential.token_hash,
            status="active",
            expires_at=_iso(now + timedelta(milliseconds=ttl_milliseconds)),
            last_used_at=None,
            rotated_at=None,
            revoked_at=None,
            created_at=timestamp,
        ),
    )


def rotate_device_refresh_credential(
    device: PairedDeviceAuthRecord,
    record: DeviceRefreshCredentialRecord,
    token: str,
    now: datetime,
    ttl_milliseconds: int,
) -> RotatedDeviceRefreshCredential:
    """校验并轮换一枚设备刷新凭据。

    返回值是**事务计划**：调用方必须原子更新 ``previous`` 并插入 ``next``。

    **检查顺序是安全语义**：凭据匹配 → 设备撤销 → 凭据状态 → 过期。
    第三项在第四项之前，意味着"已轮换且已过期"的令牌报告 ``DEVICE_CREDENTIAL_REUSED``
    而不是"过期"——因为重复提交一枚用过的令牌是**重放信号**，必须按重放处理并
    触发整族撤销，而不是提示用户重新登录。

    @param device: 当前设备状态。
    @param record: 按提交令牌哈希查得的刷新凭据记录。
    @param token: App 提交的明文刷新凭据。
    @param now: 服务端可信时钟。
    @param ttl_milliseconds: 下一代凭据有效期毫秒数。
    @returns 明文新令牌、旧记录的新状态与新记录。
    @raises DeviceAuthError: 凭据无效、设备撤销、令牌重用或已过期。
    """
    _assert_credential_ttl(ttl_milliseconds)
    if record.device_id != device.id or not verify_opaque_credential(token, record.token_hash):
        raise DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备刷新凭据无效")
    if device.status == "revoked":
        raise DeviceAuthError("DEVICE_REVOKED", "设备已经撤销")
    if record.status != "active":
        raise DeviceAuthError("DEVICE_CREDENTIAL_REUSED", "设备刷新凭据已使用或撤销")
    if _milliseconds(now) >= _milliseconds(_parse_iso(record.expires_at)):
        raise DeviceAuthError("DEVICE_CREDENTIAL_EXPIRED", "设备刷新凭据已经过期")

    next_credential = create_opaque_credential()
    timestamp = _iso(now)
    return RotatedDeviceRefreshCredential(
        token=next_credential.token,
        previous=replace(
            record, status="rotated", last_used_at=timestamp, rotated_at=timestamp
        ),
        next=DeviceRefreshCredentialRecord(
            id=str(uuid.uuid4()),
            device_id=device.id,
            family_id=record.family_id,
            generation=record.generation + 1,
            token_hash=next_credential.token_hash,
            status="active",
            expires_at=_iso(now + timedelta(milliseconds=ttl_milliseconds)),
            last_used_at=None,
            rotated_at=None,
            revoked_at=None,
            created_at=timestamp,
        ),
    )


def plan_refresh_credential_reuse_revocation(
    reused_credential: DeviceRefreshCredentialRecord,
    family_credentials: tuple[DeviceRefreshCredentialRecord, ...],
    now: datetime,
) -> RefreshCredentialFamilyRevocationPlan:
    """在检测到旧刷新凭据重用后撤销同一凭据族的全部代次。

    **连活动的最新代也要撤销**：旧令牌泄漏时，攻击者与合法客户端会竞速刷新；
    只撤销旧代等于把窗口留给攻击者。撤销整族是唯一能终止竞速的做法。

    已撤销的记录保持幂等不变（时间戳不会被刷新），这样重复检测不会产生
    无收益写入。

    @param reused_credential: 本次被再次提交的 rotated 或 revoked 凭据。
    @param family_credentials: 数据库读取的同设备、同 familyId 全部代次。
    @param now: 服务端确认重用的时间。
    @returns 可在单事务中批量持久化的同族撤销计划。
    @raises ValueError: 传入活动凭据，或族与凭据不匹配。
    """
    if reused_credential.status == "active":
        raise ValueError("活动刷新凭据不能作为重用撤销依据")
    if not any(c.id == reused_credential.id for c in family_credentials):
        raise ValueError("刷新凭据族不包含被重用的凭据")
    if any(
        c.device_id != reused_credential.device_id or c.family_id != reused_credential.family_id
        for c in family_credentials
    ):
        raise ValueError("刷新凭据重用撤销计划包含其他设备或凭据族")

    timestamp = _iso(now)
    return RefreshCredentialFamilyRevocationPlan(
        device_id=reused_credential.device_id,
        family_id=reused_credential.family_id,
        credentials=tuple(
            c if c.status == "revoked" else replace(c, status="revoked", revoked_at=timestamp)
            for c in family_credentials
        ),
    )


def _assert_credential_ttl(ttl_milliseconds: int) -> None:
    """有效期必须是正安全整数毫秒数。"""
    if (
        not isinstance(ttl_milliseconds, int)
        or isinstance(ttl_milliseconds, bool)
        or ttl_milliseconds <= 0
    ):
        raise ValueError("设备凭据有效期必须是正安全整数毫秒数")


def _milliseconds(value: datetime) -> float:
    """把时间戳折算为毫秒，便于与 ISO 文本比较。"""
    return value.timestamp() * 1000


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的 UTC 时间戳。"""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    """解析 ISO 时间戳；``Z`` 与 ``+00:00`` 均可。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

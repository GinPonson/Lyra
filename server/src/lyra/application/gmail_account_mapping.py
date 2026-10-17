"""Gmail 账户尾号到账本账户的解析。

镜像 TypeScript 版 ``application/gmail-account-mapping.ts``。

**核心安全性质：系统从不猜测尾号属于哪个账户。**

即使只有一个活跃账户的尾号匹配，也必须由用户显式确认。自动匹配看起来省事，但它把
"某封邮件属于哪个账本"这个决定交给了字符串比较——**猜错会让交易进错账本**，
而更正它意味着删除已入账的交易并重算 FIFO。

错误详情只给出**候选账户的 UUID**，不给完整券商账户键：后者含完整账号。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .app_error import ApplicationError


@dataclass(frozen=True)
class AccountRecord:
    """账本账户的最小视图。"""

    id: str
    legacy_account_key: str
    masked_suffix: str
    status: str


@dataclass(frozen=True)
class ExistingMapping:
    """已有的账户映射。"""

    id: str
    connection_id: str
    account_suffix: str
    account_id: str


@dataclass(frozen=True)
class MappingToCreate:
    """待创建的映射。"""

    connection_id: str
    account_suffix: str
    account_id: str


@dataclass(frozen=True)
class ResolvedAccountMapping:
    """解析结果。"""

    account_id: str
    legacy_account_key: str
    matched_by: Literal["existing_mapping", "explicit_selection"]
    mapping_to_create: MappingToCreate | None


def suffix_matches(account: AccountRecord, suffix: str) -> bool:
    """判断账户尾号是否匹配。

    两种匹配方式：掩码尾号完全相同，或完整账户键**以该尾号结尾**。

    @param account: 账户。
    @param suffix: 已大写化的尾号。
    @returns 是否匹配。
    """
    return (
        account.masked_suffix.strip().upper() == suffix
        or account.legacy_account_key.upper().endswith(suffix)
    )


def resolve_gmail_account_mapping(
    *,
    connection_id: str,
    account_suffix: str,
    accounts: tuple[AccountRecord, ...],
    existing_mapping: ExistingMapping | None,
    explicit_account_id: str | None,
) -> ResolvedAccountMapping:
    """解析尾号对应的账本账户。

    @param connection_id: 连接 ID。
    @param account_suffix: 邮件里提取的尾号。
    @param accounts: 全部账户。
    @param existing_mapping: 已有映射；没有为 ``None``。
    @param explicit_account_id: 用户显式指定的账户；未指定为 ``None``。
    @returns 解析结果。
    @raises ValueError: 连接 ID 或尾号为空。
    @raises ApplicationError: 需要用户确认映射（409）。
    """
    resolved_connection = _require_text(connection_id, "Gmail 连接 ID")
    suffix = _require_text(account_suffix, "Gmail 账户尾号").upper()
    active = tuple(account for account in accounts if account.status == "active")

    if existing_mapping is not None:
        matches_request = (
            existing_mapping.connection_id == resolved_connection
            and existing_mapping.account_suffix.strip().upper() == suffix
        )
        mapped = next(
            (account for account in active if account.id == existing_mapping.account_id),
            None,
        )
        # 用户显式指定了别的账户，说明他要改绑——映射不能压过他的选择。
        explicit_matches = (
            explicit_account_id is None
            or explicit_account_id == existing_mapping.account_id
        )
        if not matches_request or mapped is None or not explicit_matches:
            raise _mapping_required(active, suffix)
        return ResolvedAccountMapping(
            account_id=mapped.id,
            legacy_account_key=mapped.legacy_account_key,
            matched_by="existing_mapping",
            mapping_to_create=None,
        )

    if explicit_account_id is not None:
        selected = next(
            (
                account for account in active
                if account.id == explicit_account_id and suffix_matches(account, suffix)
            ),
            None,
        )
        if selected is not None:
            return ResolvedAccountMapping(
                account_id=selected.id,
                legacy_account_key=selected.legacy_account_key,
                matched_by="explicit_selection",
                mapping_to_create=MappingToCreate(
                    connection_id=resolved_connection, account_suffix=suffix,
                    account_id=selected.id,
                ),
            )

    # **不猜。** 即使只有一个账户的尾号匹配，也要求用户确认。
    raise _mapping_required(active, suffix)


@dataclass(frozen=True)
class MappingUpdatePlan:
    """映射改绑计划。"""

    expected_account_id: str
    next_mapping: ExistingMapping
    changed: bool


def plan_gmail_account_mapping_update(
    *,
    connection_id: str,
    mapping: ExistingMapping,
    accounts: tuple[AccountRecord, ...],
    target_account_id: str,
) -> MappingUpdatePlan:
    """规划一次映射改绑。

    @param connection_id: 期望的所属连接。
    @param mapping: 当前映射。
    @param accounts: 全部账户。
    @param target_account_id: 目标账户。
    @returns 改绑计划。
    @raises ApplicationError: 映射不属于该连接（400）或需要确认（409）。
    """
    resolved_connection = _require_text(connection_id, "Gmail 连接 ID")
    target_id = _require_text(target_account_id, "Gmail 目标账户 ID")
    suffix = _require_text(mapping.account_suffix, "Gmail 账户尾号").upper()
    if mapping.connection_id != resolved_connection:
        # 不属于当前连接的映射不能通过这条路径改——否则一次调用就能改到别处。
        raise ApplicationError(
            code="INVALID_ARGUMENT",
            message="Gmail 账户映射不属于当前连接",
            status_code=400, retryable=False,
        )
    active = tuple(account for account in accounts if account.status == "active")
    target = next(
        (
            account for account in active
            if account.id == target_id and suffix_matches(account, suffix)
        ),
        None,
    )
    if target is None:
        raise _mapping_required(active, suffix)

    if mapping.account_id == target.id:
        return MappingUpdatePlan(
            expected_account_id=mapping.account_id, next_mapping=mapping, changed=False
        )
    return MappingUpdatePlan(
        expected_account_id=mapping.account_id,
        next_mapping=ExistingMapping(
            id=mapping.id, connection_id=mapping.connection_id,
            account_suffix=mapping.account_suffix, account_id=target.id,
        ),
        changed=True,
    )


def _mapping_required(
    active: tuple[AccountRecord, ...], suffix: str
) -> ApplicationError:
    """构造"需要用户确认映射"的错误。

    **只给出候选账户的 UUID**，不给完整券商账户键——后者含完整账号。

    @returns 应用错误，详情含排序后的候选账户 ID。
    """
    candidates = sorted(
        account.id for account in active if suffix_matches(account, suffix)
    )
    return ApplicationError(
        code="ACCOUNT_MAPPING_REQUIRED",
        message="Gmail 账户尾号必须明确映射到一个活跃账户",
        status_code=409, retryable=False,
        details={"candidate_account_ids": candidates},
    )


def _require_text(value: str, label: str) -> str:
    """去除边界空白并拒绝空值。"""
    if not isinstance(value, str):
        raise ValueError(f"{label}不能为空")
    normalized = value.strip()
    if normalized == "":
        raise ValueError(f"{label}不能为空")
    return normalized

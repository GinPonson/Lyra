"""导入文件到账户的绑定。

镜像 TypeScript 版 ``import-account.ts``。

导入文件里带的是券商侧的账户标识，而 Lyra 内部用 UUID。绑定规则**刻意保守**：
只有在能唯一确定时才自动绑定，否则要求用户显式选择。

一处安全语义：绑定失败时**只返回候选 UUID**，绝不回显文件里的账户键或完整券商
标识。否则未授权调用方可以用错误信息把服务端配置的账户标识枚举出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .app_error import ApplicationError

AccountStatus = Literal["active", "archived"]


@dataclass(frozen=True)
class ImportAccountRecord:
    """参与绑定判定的账户。"""

    id: str
    legacy_account_key: str
    masked_suffix: str
    status: AccountStatus


@dataclass(frozen=True)
class ResolvedImportAccount:
    """已绑定的目标账户。"""

    account_id: str
    legacy_account_key: str
    matched_by: Literal["explicit_selection", "legacy_key"]


def resolve_import_account(
    account_hint: str,
    accounts: tuple[ImportAccountRecord, ...],
    explicit_account_id: str | None,
) -> ResolvedImportAccount:
    """把导入文件里的账户线索解析为 Lyra 账户。

    @param account_hint: 文件中的账户线索（完整旧键或含脱敏后缀的文本）。
    @param accounts: 全部账户。
    @param explicit_account_id: 用户在界面上显式选择的账户 UUID。
    @returns 已绑定的账户与该绑定的来源。
    @raises ValueError: 线索为空。
    @raises ApplicationError: 无法唯一确定账户（409），要求显式选择。
    """
    hint = account_hint.strip().upper()
    if hint == "":
        raise ValueError("导入账户线索不能为空")
    active_accounts = tuple(account for account in accounts if account.status == "active")

    if explicit_account_id is not None:
        selected = next(
            (account for account in active_accounts if account.id == explicit_account_id), None
        )
        # 显式选择也必须与文件线索相符：否则用户可以把 A 账户的文件提交到 B 账户。
        if selected is None or not _hint_matches(selected, hint):
            raise _mapping_required(active_accounts, hint)
        return ResolvedImportAccount(
            account_id=selected.id,
            legacy_account_key=selected.legacy_account_key,
            matched_by="explicit_selection",
        )

    exact_matches = tuple(
        account
        for account in active_accounts
        if account.legacy_account_key.strip().upper() == hint
    )
    if len(exact_matches) == 1:
        matched = exact_matches[0]
        return ResolvedImportAccount(
            account_id=matched.id,
            legacy_account_key=matched.legacy_account_key,
            matched_by="legacy_key",
        )
    raise _mapping_required(active_accounts, hint)


def _hint_matches(account: ImportAccountRecord, hint: str) -> bool:
    """显式选择允许完整旧键或脱敏后缀与文件线索对应。"""
    suffix = account.masked_suffix.strip().upper()
    return account.legacy_account_key.strip().upper() == hint or (
        suffix != "" and hint.endswith(suffix)
    )


def _mapping_required(
    active_accounts: tuple[ImportAccountRecord, ...],
    hint: str,
) -> ApplicationError:
    """构造绑定失败错误。

    **只返回候选 UUID**：候选集合也按线索过滤，不把全部账户列给它。
    """
    candidate_account_ids = sorted(
        account.id for account in active_accounts if _hint_matches(account, hint)
    )
    return ApplicationError(
        code="ACCOUNT_MAPPING_REQUIRED",
        message="导入文件必须明确选择匹配的活跃账户",
        status_code=409,
        retryable=False,
        details={"candidate_account_ids": candidate_account_ids},
    )

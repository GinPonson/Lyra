"""按币种分组的公开结果信封。

镜像 TypeScript 版工具运行时的 ``createCurrencyGroupedResult``。

**为什么必须分组**：``USD`` 与 ``HKD`` 的金额直接相加在会计上是错的——得到的
数字不对应任何真实资产。因此金额类分析必须先按账户基础币种切分，在**每个币种
内部**独立计算，再并列返回。

**最容易写错的一点**：分组后必须用**该币种的子集**去计算，而不是拿全量快照算完
再按币种贴标签。后者会把其它币种的数字混进本组，且不会报错。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from ...infrastructure.db.ledger_repository import LedgerAccount, LedgerSnapshot
from .types import AccountScope

T = TypeVar("T")


@dataclass(frozen=True)
class CurrencyGroup:
    """一个基础币种内可安全参与金额合计的账本子集的计算结果。"""

    currency: str
    account_ids: tuple[str, ...]
    data: Any


@dataclass(frozen=True)
class CurrencyGroupedResult:
    """按币种分组的信封。

    ``aggregate_currency`` 只在范围内**恰好只有一种币种**时给出；跨币种时为
    ``None``，表示不存在一个可对外声明的"合计币种"。
    """

    aggregate_currency: str | None
    groups: tuple[CurrencyGroup, ...]


def group_by_currency(
    snapshot: LedgerSnapshot,
    account_scope: AccountScope,
    calculate: Callable[[LedgerSnapshot], T],
) -> CurrencyGroupedResult:
    """按账户基础币种切分账本并逐组计算。

    @param snapshot: 全量账本快照（含账户与币种）。
    @param account_scope: 显式账户范围。
    @param calculate: 针对单一币种子集的计算函数。
    @returns 分组信封；范围内没有账户时 ``groups`` 为空。
    """
    scoped_accounts = tuple(
        account for account in snapshot.accounts if _in_scope(account, account_scope)
    )
    grouped = _group_accounts(scoped_accounts)

    groups = tuple(
        CurrencyGroup(
            currency=currency,
            account_ids=tuple(account.id for account in accounts),
            # 关键：用该币种的子集计算，而不是全量快照。
            data=calculate(_subset(snapshot, accounts)),
        )
        for currency, accounts in grouped
    )
    currencies = {currency for currency, _ in grouped}
    return CurrencyGroupedResult(
        aggregate_currency=next(iter(currencies)) if len(currencies) == 1 else None,
        groups=groups,
    )


def _group_accounts(accounts: tuple[LedgerAccount, ...]) -> list[tuple[str, tuple[LedgerAccount, ...]]]:
    """按基础币种分组，并保持币种名的稳定顺序。"""
    buckets: dict[str, list[LedgerAccount]] = {}
    for account in accounts:
        buckets.setdefault(account.base_currency, []).append(account)
    return [(currency, tuple(buckets[currency])) for currency in sorted(buckets)]


def _subset(snapshot: LedgerSnapshot, accounts: tuple[LedgerAccount, ...]) -> LedgerSnapshot:
    """取出仅属于这些账户的账本子集。"""
    keys = {account.legacy_account_key for account in accounts}
    return replace(
        snapshot,
        accounts=accounts,
        transactions=tuple(t for t in snapshot.transactions if t.account in keys),
        open_lots=tuple(lot for lot in snapshot.open_lots if lot.account in keys),
        realized_records=tuple(r for r in snapshot.realized_records if r.account in keys),
    )


def _in_scope(account: LedgerAccount, scope: AccountScope) -> bool:
    """判断账户是否落在范围内。"""
    return scope.kind == "all" or scope.account == account.legacy_account_key

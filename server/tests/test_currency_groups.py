"""按币种分组信封的测试。

最重要的断言是 :func:`test_each_group_calculates_on_its_own_subset`：分组后必须
用**该币种的子集**计算。若拿全量快照算完再贴币种标签，其它币种的数字会混进本组，
而且**不会报错**——这类错误只有在真实多币种账本上才会被发现，因此必须有测试钉住。
"""

from __future__ import annotations

from decimal import Decimal

from lyra.domain.analytics.currency_groups import group_by_currency
from lyra.domain.analytics.types import AccountScope
from lyra.domain.fifo.types import Lot
from lyra.domain.ledger.types import ParsedTransaction
from lyra.infrastructure.db.ledger_repository import LedgerAccount, LedgerSnapshot

USD_ACCOUNT = LedgerAccount(
    id="11111111-1111-1111-1111-111111111111", legacy_account_key="USD001", base_currency="USD"
)
HKD_ACCOUNT = LedgerAccount(
    id="22222222-2222-2222-2222-222222222222", legacy_account_key="HKD002", base_currency="HKD"
)


def _transaction(account: str, symbol: str, amount: str) -> ParsedTransaction:
    """构造一条最小可用交易。"""
    return ParsedTransaction(
        account=account, source_type="csv", source_reference="src",
        record_date="2026-01-01", transaction_date="2026-01-01", action="Buy",
        raw_symbol=symbol, description=symbol, asset_type="stock", underlying=symbol,
        expiry=None, strike=None, option_type=None,
        quantity=Decimal(1), price=Decimal(1), fees=None, amount=Decimal(amount),
        source_hash="h" * 64, sequence=1, occurrence=1,
        business_hash="b" * 64, transaction_hash=f"t-{symbol}",
    )


def _snapshot(accounts: tuple[LedgerAccount, ...], transactions: tuple[ParsedTransaction, ...]) -> LedgerSnapshot:
    """构造只含所需字段的快照。"""
    return LedgerSnapshot(
        accounts=accounts, transactions=transactions, open_lots=(), realized_records=()
    )


def test_single_currency_yields_aggregate_currency() -> None:
    """范围内只有一种币种时，可以对外声明合计币种。"""
    snapshot = _snapshot((USD_ACCOUNT,), (_transaction("USD001", "AAA", "10"),))

    result = group_by_currency(snapshot, AccountScope(kind="all"), lambda s: len(s.transactions))

    assert result.aggregate_currency == "USD"
    assert [group.currency for group in result.groups] == ["USD"]
    assert result.groups[0].data == 1


def test_multiple_currencies_have_no_aggregate_currency() -> None:
    """跨币种时不得给出合计币种——不存在这样一个币种。"""
    snapshot = _snapshot(
        (USD_ACCOUNT, HKD_ACCOUNT),
        (_transaction("USD001", "AAA", "10"), _transaction("HKD002", "BBB", "20")),
    )

    result = group_by_currency(snapshot, AccountScope(kind="all"), lambda s: len(s.transactions))

    assert result.aggregate_currency is None
    assert [group.currency for group in result.groups] == ["HKD", "USD"]


def test_each_group_calculates_on_its_own_subset() -> None:
    """**核心断言**：每组拿到的是该币种的子集，不是全量快照。

    若实现拿全量快照计算，两组都会得到 2——错误不会以异常形式出现。
    """
    snapshot = _snapshot(
        (USD_ACCOUNT, HKD_ACCOUNT),
        (
            _transaction("USD001", "AAA", "10"),
            _transaction("USD001", "BBB", "20"),
            _transaction("HKD002", "CCC", "30"),
        ),
    )

    def count(snapshot: LedgerSnapshot) -> int:
        return len(snapshot.transactions)

    result = group_by_currency(snapshot, AccountScope(kind="all"), count)

    by_currency = {group.currency: group.data for group in result.groups}
    assert by_currency == {"USD": 2, "HKD": 1}


def test_subset_also_restricts_accounts_and_lots() -> None:
    """子集必须同时收窄账户列表与未平批次，否则引用会越界。"""
    lot_usd = Lot(
        account="USD001", symbol_key="AAA", asset_type="stock", underlying="AAA",
        expiry=None, strike=None, option_type=None, direction="long",
        open_date="2026-01-01", open_price=Decimal(1), open_quantity=Decimal(1),
        remaining_quantity=Decimal(1), open_fees=Decimal(0),
        open_transaction="t", fees_allocated=Decimal(0),
    )
    snapshot = LedgerSnapshot(
        accounts=(USD_ACCOUNT, HKD_ACCOUNT),
        transactions=(),
        open_lots=(lot_usd,),
        realized_records=(),
    )

    result = group_by_currency(
        snapshot, AccountScope(kind="all"), lambda s: (len(s.accounts), len(s.open_lots))
    )

    # 每个币种组保留**它自己的**账户，但只保留该币种的批次。
    # HKD 组没有批次，因此是 (1, 0)，而不是 (0, 0)。
    assert result.groups[0].data == (1, 0)
    assert result.groups[1].data == (1, 1)


def test_account_ids_are_exposed_for_grouping() -> None:
    """信封按内部 UUID 暴露账户引用，而不是脱敏键。"""
    snapshot = _snapshot((USD_ACCOUNT, HKD_ACCOUNT), ())

    result = group_by_currency(snapshot, AccountScope(kind="all"), lambda s: None)

    assert result.groups[0].account_ids == (HKD_ACCOUNT.id,)
    assert result.groups[1].account_ids == (USD_ACCOUNT.id,)


def test_account_scope_restricts_groups() -> None:
    """账户范围必须先作用于分组，范围外币种不得出现在信封里。"""
    snapshot = _snapshot(
        (USD_ACCOUNT, HKD_ACCOUNT),
        (_transaction("USD001", "AAA", "10"), _transaction("HKD002", "BBB", "20")),
    )

    result = group_by_currency(
        snapshot, AccountScope(kind="account", account="USD001"), lambda s: len(s.transactions)
    )

    assert result.aggregate_currency == "USD"
    assert [group.currency for group in result.groups] == ["USD"]


def test_empty_scope_yields_empty_groups() -> None:
    """范围内没有账户时返回空信封，而不是回落到全部币种。"""
    snapshot = _snapshot((USD_ACCOUNT,), (_transaction("USD001", "AAA", "10"),))

    result = group_by_currency(
        snapshot, AccountScope(kind="account", account="NOPE"), lambda s: len(s.transactions)
    )

    assert result.groups == ()
    assert result.aggregate_currency is None

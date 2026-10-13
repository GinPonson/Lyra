"""账户总览。

镜像 TypeScript 版 ``summary.ts``。金额按现有 DuckDB schema 的小数位输出
（Amount 与 realized PnL 为 4 位、费用为 6 位），这样迁移后客户端看到的
字符串与现在完全一致——数值相同但位数不同同样会被客户端当作变化。
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from ..fifo.types import FifoReplayResult
from ..ledger.types import ParsedTransaction
from .dates import is_account_in_scope
from .positions import aggregate_positions
from .types import AccountScope, AccountSummary

# 会计上计入股息合计的 Schwab 动作。
DIVIDEND_ACTIONS: frozenset[str] = frozenset({
    "Qualified Dividend", "Non-Qualified Div", "Pr Yr Non-Qual Div",
})
# 会计上计入税项合计的 Schwab 动作。
TAX_ACTIONS: frozenset[str] = frozenset({
    "NRA Tax Adj", "Pr Yr NRA Tax", "Foreign Tax Paid",
})

_AMOUNT_QUANTUM = Decimal("0.0001")


def calculate_account_summary(
    transactions: tuple[ParsedTransaction, ...],
    fifo: FifoReplayResult,
    account_scope: AccountScope,
) -> AccountSummary:
    """按显式账户范围计算总览口径。

    @param transactions: 标准交易账本。
    @param fifo: 与该账本版本对应的 FIFO 重放结果。
    @param account_scope: 单账户或全部账户，调用方不可省略。
    @returns 精确金额已序列化为固定小数字符串的总览。
    """
    scoped_transactions = [
        transaction for transaction in transactions
        if is_account_in_scope(transaction.account, account_scope)
    ]
    scoped_realized = [
        record for record in fifo.realized_records
        if is_account_in_scope(record.account, account_scope)
    ]
    scoped_lots = [
        lot for lot in fifo.open_lots
        if is_account_in_scope(lot.account, account_scope)
    ]

    realized_stock = _sum_decimals([
        record.pnl for record in scoped_realized if record.asset_type == "stock"
    ])
    realized_option = _sum_decimals([
        record.pnl for record in scoped_realized if record.asset_type == "option"
    ])
    dates = sorted(transaction.transaction_date for transaction in scoped_transactions)

    return AccountSummary(
        net_deposits=_format_amount(_sum_transaction_amounts([
            transaction for transaction in scoped_transactions
            if transaction.action == "MoneyLink Transfer"
        ])),
        realized_pnl_stock=_format_amount(realized_stock),
        realized_pnl_option=_format_amount(realized_option),
        realized_pnl_total=_format_amount(realized_stock + realized_option),
        open_positions=len(aggregate_positions(tuple(scoped_lots), account_scope)),
        total_fees=format(
            _sum_decimals([
                transaction.fees for transaction in scoped_transactions
            ]).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            "f",
        ),
        dividends=_format_amount(_sum_transaction_amounts([
            transaction for transaction in scoped_transactions
            if transaction.action in DIVIDEND_ACTIONS
        ])),
        interest=_format_amount(_sum_transaction_amounts([
            transaction for transaction in scoped_transactions
            if transaction.action == "Credit Interest"
        ])),
        taxes=_format_amount(_sum_transaction_amounts([
            transaction for transaction in scoped_transactions
            if transaction.action in TAX_ACTIONS
        ])),
        cash_balance=_format_amount(_sum_transaction_amounts(scoped_transactions)),
        txn_count=len(scoped_transactions),
        date_range=None if not dates else f"{dates[0]} ~ {dates[-1]}",
    )


def _sum_transaction_amounts(transactions: list[ParsedTransaction]) -> Decimal:
    """合计交易 Amount；空值与 SQL ``sum`` 一样不贡献数值。"""
    return _sum_decimals([transaction.amount for transaction in transactions])


def _sum_decimals(values: list[Decimal | None]) -> Decimal:
    """合计可空 Decimal，并显式以 Decimal 零作为空集合起始值。"""
    total = Decimal(0)
    for value in values:
        if value is not None:
            total += value
    return total


def _format_amount(value: Decimal) -> str:
    """Amount 与 realized PnL 按既有 schema 的四位小数输出。"""
    return format(value.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), "f")

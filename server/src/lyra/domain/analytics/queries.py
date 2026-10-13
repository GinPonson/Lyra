"""交易流水、已实现明细与现金流水查询。

镜像 TypeScript 版 ``queries.ts`` 的**默认查询路径**：过滤、排序、页长限制
与合计口径。

**本阶段未实现游标分页。** TypeScript 侧的 ``nextCursor`` 是不透明游标，属于
接口层契约；它的具体编码会在 M4（只读 REST API）按协议 2 的既有形状一并确定。
此处不实现，也不返回占位值——返回一个"看起来能翻页"的假游标比没有更危险。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..fifo.types import RealizedRecord
from ..numeric import fixed_text
from ..ledger.types import ParsedTransaction
from .dates import is_account_in_scope, is_date_in_range, validate_date_range
from .cursor import (
    cashflow_row_key,
    detail_filter_fingerprint,
    paginate,
    paginate_detail,
    realized_row_key,
    trade_filter_fingerprint,
)
from .types import (
    AccountScope,
    CashflowQueryResult,
    CashflowRow,
    RealizedQueryResult,
    RealizedRow,
    TradeQueryResult,
    TradeRow,
)

# 默认页长与上限，与现有实现一致。
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

_AMOUNT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class TradeQueryFilters:
    """交易流水查询的可选过滤条件。"""

    symbol: str | None = None
    action: str | None = None
    source_type: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    limit: int | None = None
    # 定位游标；``None`` 表示从第一页开始。
    cursor: str | None = None


@dataclass(frozen=True)
class RealizedQueryFilters:
    """已实现明细查询的可选过滤条件。"""

    symbol: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    limit: int | None = None
    # 定位游标；``None`` 表示从第一页开始。
    cursor: str | None = None


@dataclass(frozen=True)
class CashflowQueryFilters:
    """现金流水查询的可选过滤条件。"""

    source_type: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    limit: int | None = None
    # 定位游标；``None`` 表示从第一页开始。
    cursor: str | None = None


def query_trades(
    transactions: tuple[ParsedTransaction, ...],
    account_scope: AccountScope,
    filters: TradeQueryFilters | None = None,
) -> TradeQueryResult:
    """查询标准交易流水。

    @param transactions: 标准交易账本快照。
    @param account_scope: 必填的账户范围。
    @param filters: 标的、动作、来源类型、闭区间日期与最大返回数。
    @returns 确定性排序的当前页交易。
    @raises ValueError: 日期无效或范围反向，或 limit 越界。
    """
    resolved = filters if filters is not None else TradeQueryFilters()
    validate_date_range(resolved.from_date, resolved.to_date)
    limit = _require_page_limit(resolved.limit)
    symbol = resolved.symbol.upper() if resolved.symbol is not None else None

    filtered = [
        transaction for transaction in transactions
        if is_account_in_scope(transaction.account, account_scope)
        and (
            symbol is None
            or transaction.underlying == symbol
            or transaction.raw_symbol == symbol
        )
        and (resolved.action is None or transaction.action == resolved.action)
        and (resolved.source_type is None or transaction.source_type == resolved.source_type)
        and is_date_in_range(transaction.transaction_date, resolved.from_date, resolved.to_date)
    ]
    filtered.sort(key=_trade_order)

    # **游标分页**：指纹绑定账户范围与全部筛选条件，防止把一个查询的游标用到另一个上
    # （那会从错误的位置继续返回，且不报错）。
    fingerprint = trade_filter_fingerprint(
        account_scope, symbol=symbol, action=resolved.action,
        source_type=resolved.source_type, from_date=resolved.from_date,
        to_date=resolved.to_date,
    )
    page = paginate(
        filtered, limit=limit, cursor=resolved.cursor,
        fingerprint=fingerprint, hash_of=lambda txn: txn.transaction_hash,
    )

    rows = tuple(
        TradeRow(
            txn_hash=transaction.transaction_hash,
            account=transaction.account,
            record_date=transaction.record_date,
            txn_date=transaction.transaction_date,
            action=transaction.action,
            raw_symbol=transaction.raw_symbol,
            description=transaction.description,
            asset_type=transaction.asset_type,
            underlying=transaction.underlying,
            expiry=transaction.expiry,
            strike=transaction.strike,
            option_type=transaction.option_type,
            quantity=transaction.quantity,
            price=transaction.price,
            fees=transaction.fees,
            amount=transaction.amount,
            source_hash=transaction.source_hash,
            seq=transaction.sequence,
        )
        for transaction in page.rows
    )
    return TradeQueryResult(
        rows=rows, count=len(rows), next_cursor=page.next_cursor
    )


def query_realized(
    records: tuple[RealizedRecord, ...],
    account_scope: AccountScope,
    filters: RealizedQueryFilters | None = None,
) -> RealizedQueryResult:
    """查询已实现明细。

    合计取**过滤后的全量**，不是当前页——否则翻页时合计会随页变化。

    @param records: FIFO 已实现明细。
    @param account_scope: 必填的账户范围。
    @param filters: 标的、闭区间平仓日期与最大返回数。
    @returns 平仓日倒序的当前页明细与四位小数损益合计。
    @raises ValueError: 日期无效或范围反向，或 limit 越界。
    """
    resolved = filters if filters is not None else RealizedQueryFilters()
    validate_date_range(resolved.from_date, resolved.to_date)
    limit = _require_page_limit(resolved.limit)
    symbol = resolved.symbol.upper() if resolved.symbol is not None else None

    filtered = [
        record for record in records
        if is_account_in_scope(record.account, account_scope)
        and (
            symbol is None
            or record.underlying == symbol
            or record.symbol_key == symbol
        )
        and is_date_in_range(record.close_date, resolved.from_date, resolved.to_date)
    ]
    filtered.sort(key=_realized_order)

    # **明细族游标**：载荷里带 ``query`` 判别字段，指纹里也含查询名——两道独立防线
    # 共同阻止"把 cashflow 的游标用在 realized 上"（那会返回错位的行且不报错）。
    page = paginate_detail(
        filtered, limit=limit, cursor=resolved.cursor, query="realized",
        fingerprint=detail_filter_fingerprint(
            "realized", account_scope, symbol=symbol,
            fromDate=resolved.from_date, toDate=resolved.to_date,
        ),
        row_key_of=realized_row_key,
    )

    rows = tuple(
        RealizedRow(
            account=record.account,
            symbol_key=record.symbol_key,
            asset_type=record.asset_type,
            underlying=record.underlying,
            direction=record.direction,
            open_date=record.open_date,
            close_date=record.close_date,
            qty=record.quantity,
            open_price=record.open_price,
            close_price=record.close_price,
            open_fees=record.open_fees,
            close_fees=record.close_fees,
            pnl=record.pnl,
            close_action=record.close_action,
            open_txn=record.open_transaction,
            close_txn=record.close_transaction,
        )
        for record in page.rows
    )
    return RealizedQueryResult(
        rows=rows,
        count=len(rows),
        # **合计与总数都基于全部筛选行，而非当前页。** 参考实现同样如此
        # （`sumDecimals(filtered...)` 与 `totalCount: filtered.length`）——否则翻到
        # 第二页时合计会变小，客户端会以为数据出了问题。
        total_pnl=_fixed_four(_sum_decimals([record.pnl for record in filtered])),
        total_count=len(filtered),
        next_cursor=page.next_cursor,
    )


def query_cashflow(
    transactions: tuple[ParsedTransaction, ...],
    account_scope: AccountScope,
    filters: CashflowQueryFilters | None = None,
) -> CashflowQueryResult:
    """查询现金类交易并按 Action 汇总现金变动。

    @param transactions: 标准交易账本快照。
    @param account_scope: 必填的账户范围。
    @param filters: 来源类型、闭区间交易日期与最大返回数。
    @returns 日期倒序的当前页明细、总额与分动作四位小数合计。
    @raises ValueError: 日期无效或范围反向，或 limit 越界。
    """
    resolved = filters if filters is not None else CashflowQueryFilters()
    validate_date_range(resolved.from_date, resolved.to_date)
    limit = _require_page_limit(resolved.limit)

    filtered = [
        transaction for transaction in transactions
        if transaction.asset_type == "cash"
        and is_account_in_scope(transaction.account, account_scope)
        and (resolved.source_type is None or transaction.source_type == resolved.source_type)
        and is_date_in_range(transaction.transaction_date, resolved.from_date, resolved.to_date)
    ]
    filtered.sort(key=_trade_order)

    # 明细族游标，``query="cashflow"``；行键直接用交易哈希（与参考实现一致）。
    page = paginate_detail(
        filtered, limit=limit, cursor=resolved.cursor, query="cashflow",
        fingerprint=detail_filter_fingerprint(
            "cashflow", account_scope, sourceType=resolved.source_type,
            fromDate=resolved.from_date, toDate=resolved.to_date,
        ),
        row_key_of=cashflow_row_key,
    )

    rows = tuple(
        CashflowRow(
            account=transaction.account,
            txn_date=transaction.transaction_date,
            action=transaction.action,
            description=transaction.description,
            amount=transaction.amount,
            txn_hash=transaction.transaction_hash,
        )
        for transaction in filtered[:limit]
    )

    by_action: dict[str, Decimal] = {}
    for transaction in filtered:
        if transaction.amount is None:
            continue
        by_action[transaction.action] = (
            by_action.get(transaction.action, Decimal(0)) + transaction.amount
        )

    return CashflowQueryResult(
        rows=rows,
        count=len(rows),
        total=_fixed_four(_sum_decimals([t.amount for t in filtered])),
        by_action={action: _fixed_four(amount) for action, amount in by_action.items()},
        # **合计与总数基于全部筛选行**，与参考实现一致（翻页不会让合计变小）。
        total_count=len(filtered),
        next_cursor=page.next_cursor,
    )


def _require_page_limit(value: int | None) -> int:
    """校验页长；越界直接报错而不是静默截断。"""
    limit = DEFAULT_PAGE_LIMIT if value is None else value
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_PAGE_LIMIT} 的整数")
    return limit


def _trade_order(transaction: ParsedTransaction) -> tuple[object, ...]:
    """交易与现金流水的稳定次序：生效日倒序，其后为文件内顺序与来源键。"""
    return (
        _descending(transaction.transaction_date),
        transaction.sequence,
        transaction.account,
        transaction.source_type,
        transaction.source_reference,
        transaction.occurrence,
        transaction.transaction_hash,
    )


def _realized_order(record: RealizedRecord) -> tuple[object, ...]:
    """已实现明细的稳定次序：平仓日倒序，其后为交易哈希与账户。"""
    return (
        _descending(record.close_date),
        record.close_transaction,
        record.open_transaction,
        record.account,
    )


class _Descending:
    """把字符串按降序参与排序的最小包装；``sorted`` 无法直接对字符串取反。"""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: _Descending) -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Descending) and self.value == other.value


def _descending(value: str) -> _Descending:
    """构造降序排序键。"""
    return _Descending(value)


def _sum_decimals(values: list[Decimal | None]) -> Decimal:
    """合计可空 Decimal；空字段与 SQL ``sum`` 一样不贡献数值。"""
    total = Decimal(0)
    for value in values:
        if value is not None:
            total += value
    return total


def _fixed_four(value: Decimal) -> str:
    """委托共享实现（见 :func:`lyra.domain.numeric.fixed_text`）。"""
    return fixed_text(value, 4)


def _fixed_four_legacy(value: Decimal) -> str:
    """按四位小数渲染金额，**舍入方式与 decimal.js 的 ``toFixed`` 一致**。

    ``toFixed`` 是"四舍五入（平局远离零）"，**不是银行家舍入**：

    | 值 | ``toFixed(4)`` | ``ROUND_HALF_EVEN`` |
    | --- | --- | --- |
    | ``1.23445`` | ``1.2345`` | ``1.2344`` |
    | ``0.00005`` | ``0.0001`` | ``0.0000`` |

    差异只出现在**恰好落在中点**的值上，而账本金额恰好落到第 5 位为 5 并不罕见
    （费用按比例分摊时尤其常见）。Python 的 ``ROUND_HALF_UP`` 语义正是"远离零"。

    @param value: 待渲染的金额。
    @returns 四位小数的定点文本。
    """
    quantized = value.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized == 0:
        # **避免 ``-0.0000``**：decimal.js 的 ``toFixed`` 对结果为 0 的值给出
        # ``0.0000``，而 Python 会保留负号。两者会被客户端当成不同的数值文本。
        quantized = abs(quantized)
    return format(quantized, "f")

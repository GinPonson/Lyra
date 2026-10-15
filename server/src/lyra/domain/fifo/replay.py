"""FIFO 全量重放。

镜像 TypeScript 版 ``replay.ts``。重放顺序是**业务语义**而非实现细节：
生效日升序 → 同日物理顺序（``sequence``）降序 → 交易哈希升序。Schwab 导出
最新在前，因此同一天里物理位置更靠后的行发生得更早。

任何超卖、方向无法确定或顺序不可证明都立即抛错，不返回部分结果——错误的
持仓与损益比明确的失败危险得多。
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from ..ledger.types import ParsedTransaction
from ..numeric import decimal_text
from .types import FifoError, FifoReplayResult, Lot, LotDirection, RealizedRecord

# 开仓动作到持仓方向的映射，与现有 Python FIFO 保持一致。
OPEN_DIRECTIONS: dict[str, LotDirection] = {
    "Buy": "long",
    "Buy to Open": "long",
    "Sell to Open": "short",
}

# 平仓动作到被平持仓方向的映射；Expired 需要从现有批次推断方向。
CLOSE_DIRECTIONS: dict[str, LotDirection] = {
    "Sell": "long",
    "Sell to Close": "long",
    "Buy to Close": "short",
    "Assigned": "short",
}

# 已实现损益与合计的量化精度。
_PNL_QUANTUM = Decimal("0.0001")


def replay_fifo(transactions: tuple[ParsedTransaction, ...]) -> FifoReplayResult:
    """按 Schwab 语义全量重放标准交易，生成未平批次与已实现损益。

    输入不会被修改，因此对同一交易集合重复调用会得到同一结果。

    @param transactions: 已完成标准化与 occurrence 编号的全部账本交易。
    @returns 可由数据库层原子持久化的 FIFO 结果。
    @raises FifoError: 顺序不可证明、超卖、无持仓可平或动作不受支持。
    """
    assert_replay_order_is_provable(transactions)

    position_transactions = sorted(
        (
            transaction for transaction in transactions
            if transaction.asset_type in ("stock", "option")
        ),
        key=_replay_order_key,
    )
    books: dict[tuple[str, str | None], list[Lot]] = {}
    realized_records: list[RealizedRecord] = []

    for transaction in position_transactions:
        direction_to_open = OPEN_DIRECTIONS.get(transaction.action)
        if direction_to_open is not None:
            key = (transaction.account, transaction.raw_symbol)
            books.setdefault(key, []).append(_open_lot(transaction, direction_to_open))
            continue

        direction_to_close = CLOSE_DIRECTIONS.get(transaction.action)
        if direction_to_close is not None:
            close_price = transaction.price if transaction.price is not None else Decimal(0)
            _close_position(
                transaction, direction_to_close, close_price, books, realized_records
            )
            continue

        if transaction.action == "Expired":
            book = books.get((transaction.account, transaction.raw_symbol), [])
            directions = {
                lot.direction for lot in book if lot.remaining_quantity > 0
            }
            if len(directions) != 1:
                current = "无持仓" if not directions else ",".join(sorted(directions))
                raise FifoError(
                    f"{transaction.transaction_date} Expired "
                    f"{_display_symbol(transaction.raw_symbol)}: "
                    f"无法确定持仓方向(现有方向: {current})"
                )
            direction = next(iter(directions))
            _close_position(transaction, direction, Decimal(0), books, realized_records)
            continue

        raise FifoError(f"未处理的持仓 Action: '{transaction.action}'")

    open_lots = tuple(
        lot
        for book in books.values()
        for lot in book
        if lot.remaining_quantity > 0
    )
    max_transaction_date = max(
        (transaction.transaction_date for transaction in transactions), default=None
    )
    warnings = tuple(
        f"期权 {_display_symbol(lot.symbol_key)} 已于 {lot.expiry} 到期,"
        f"仍有 {decimal_text(lot.remaining_quantity)} 张未平"
        for lot in open_lots
        if lot.asset_type == "option"
        and lot.expiry is not None
        and max_transaction_date is not None
        and lot.expiry < max_transaction_date
    )

    total_realized_pnl = sum(
        (record.pnl for record in realized_records), Decimal(0)
    ).quantize(_PNL_QUANTUM, rounding=ROUND_HALF_EVEN)

    return FifoReplayResult(
        transactions_replayed=len(position_transactions),
        open_lots=open_lots,
        realized_records=tuple(realized_records),
        total_realized_pnl=format(total_realized_pnl, "f"),
        warnings=warnings,
    )


def _open_lot(transaction: ParsedTransaction, direction: LotDirection) -> Lot:
    """创建开仓批次，并验证标准化器承诺的必填数值仍然存在。

    @raises FifoError: 缺数量或价格，或资产类型不是股票/期权。
    """
    if transaction.quantity is None or transaction.price is None:
        raise FifoError(
            f"{transaction.transaction_date} {transaction.action} "
            f"{_display_symbol(transaction.raw_symbol)}: 开仓交易缺少数量或价格"
        )
    if transaction.asset_type not in ("stock", "option"):
        raise FifoError(f"开仓交易资产类型无效: {transaction.asset_type}")

    return Lot(
        account=transaction.account,
        symbol_key=transaction.raw_symbol,
        asset_type=transaction.asset_type,
        underlying=transaction.underlying,
        expiry=transaction.expiry,
        strike=transaction.strike,
        option_type=transaction.option_type,
        direction=direction,
        open_date=transaction.transaction_date,
        open_price=transaction.price,
        open_quantity=transaction.quantity,
        remaining_quantity=transaction.quantity,
        open_fees=transaction.fees if transaction.fees is not None else Decimal(0),
        open_transaction=transaction.transaction_hash,
        fees_allocated=Decimal(0),
    )


def _close_position(
    transaction: ParsedTransaction,
    direction: LotDirection,
    close_price: Decimal,
    books: dict[tuple[str, str | None], list[Lot]],
    realized_records: list[RealizedRecord],
) -> None:
    """按 FIFO 消耗指定方向的批次，并为每段消耗生成 realized 记录。

    @raises FifoError: 缺数量、没有可平持仓，或消耗数量超过未平数量。
    """
    if transaction.quantity is None:
        raise FifoError(
            f"{transaction.transaction_date} {transaction.action} "
            f"{_display_symbol(transaction.raw_symbol)}: 平仓交易缺少数量"
        )
    key = (transaction.account, transaction.raw_symbol)
    book = books.setdefault(key, [])
    where = (
        f"{transaction.transaction_date} {transaction.action} "
        f"{_display_symbol(transaction.raw_symbol)}"
    )
    directional_book = [
        lot for lot in book
        if lot.direction == direction and lot.remaining_quantity > 0
    ]
    if not directional_book:
        raise FifoError(f"{where}: 没有可平的 {direction} 持仓")

    # 一个账户与标的可能同时持有多空两组批次；可用量与 FIFO 顺序都必须只看
    # 本次 Action 指定的方向，不能让一侧的超额平仓消耗另一侧的批次。
    consumed = _consume(directional_book, transaction.quantity, where)
    # _consume 操作方向筛选后的队列；同步清掉原账本中已耗尽的 Lot，保留相反方向。
    book[:] = [lot for lot in book if lot.remaining_quantity > 0]
    close_fees_total = transaction.fees if transaction.fees is not None else Decimal(0)
    close_fees_allocated = Decimal(0)
    multiplier = 100 if transaction.asset_type == "option" else 1

    for index, (lot, quantity) in enumerate(consumed):
        open_fees = _allocate_open_fee(lot, quantity)
        is_last = index == len(consumed) - 1
        if is_last:
            close_fees = close_fees_total - close_fees_allocated
        else:
            # 逐段按数量比例分摊；最后一段吃掉尾差，避免舍入漂移。
            close_fees = close_fees_total * quantity / transaction.quantity
            close_fees_allocated += close_fees

        gross = (
            (close_price - lot.open_price) * quantity * multiplier
            if lot.direction == "long"
            else (lot.open_price - close_price) * quantity * multiplier
        )
        pnl = (gross - open_fees - close_fees).quantize(
            _PNL_QUANTUM, rounding=ROUND_HALF_EVEN
        )

        realized_records.append(RealizedRecord(
            account=lot.account,
            symbol_key=lot.symbol_key,
            asset_type=lot.asset_type,
            underlying=lot.underlying,
            direction=lot.direction,
            open_date=lot.open_date,
            close_date=transaction.transaction_date,
            quantity=quantity,
            open_price=lot.open_price,
            close_price=close_price,
            open_fees=open_fees,
            close_fees=close_fees,
            pnl=pnl,
            close_action=transaction.action,
            open_transaction=lot.open_transaction,
            close_transaction=transaction.transaction_hash,
        ))


def _consume(
    book: list[Lot],
    requested_quantity: Decimal,
    where: str,
) -> list[tuple[Lot, Decimal]]:
    """从一个合约的 FIFO 队列消耗数量；队列不足时不继续计算。

    @raises FifoError: 未平数量不足，或队列意外为空。
    """
    available = sum((lot.remaining_quantity for lot in book), Decimal(0))
    if available < requested_quantity:
        first = book[0] if book else None
        raise FifoError(
            f"{where}: 平仓数量 {decimal_text(requested_quantity)} "
            f"超过未平数量 {decimal_text(available)}"
            f"(合约 {_display_symbol(first.symbol_key if first else None)},"
            f"方向 {first.direction if first else '?'})"
        )

    consumed: list[tuple[Lot, Decimal]] = []
    left = requested_quantity
    while left > 0:
        if not book:
            raise FifoError(f"{where}: FIFO 队列意外为空")
        lot = book[0]
        quantity = min(lot.remaining_quantity, left)
        lot.remaining_quantity -= quantity
        left -= quantity
        consumed.append((lot, quantity))
        if lot.remaining_quantity == 0:
            book.pop(0)
    return consumed


def _allocate_open_fee(lot: Lot, quantity: Decimal) -> Decimal:
    """按消耗比例分摊开仓费；批次耗尽时一次性分配剩余尾差。"""
    if lot.remaining_quantity == 0:
        allocated = lot.open_fees - lot.fees_allocated
    else:
        allocated = lot.open_fees * quantity / lot.open_quantity
    lot.fees_allocated += allocated
    return allocated


def assert_replay_order_is_provable(transactions: tuple[ParsedTransaction, ...]) -> None:
    """检查同账户、同日、同合约的顺序是否来自同一可比较文件快照。

    重叠导出的同一天若来自两个未对齐的快照，文件内物理顺序不可比较；
    此时必须拒绝重放，而不是按不可靠的顺序算出持仓。

    @raises FifoError: 存在来自多个来源哈希的同一 (账户, 日期, 合约) 分组。
    """
    groups: dict[tuple[str, str, str | None], set[str]] = {}
    for transaction in transactions:
        if transaction.asset_type not in ("stock", "option"):
            continue
        key = (transaction.account, transaction.transaction_date, transaction.raw_symbol)
        groups.setdefault(key, set()).add(transaction.source_hash)

    ambiguous = sorted(
        (
            (key, sources) for key, sources in groups.items() if len(sources) > 1
        ),
        key=lambda item: (item[0][0], item[0][1], _display_symbol(item[0][2])),
    )
    if not ambiguous:
        return
    (account, transaction_date, symbol), sources = ambiguous[0]
    raise FifoError(
        f"{transaction_date} {_display_symbol(symbol)}: "
        f"同日交易来自 {len(sources)} 个未对齐的 CSV 快照"
        f"(账户 {account})，无法可靠确定 FIFO 顺序；"
        "请导入覆盖该日全部交易的最新导出文件"
    )


def _replay_order_key(transaction: ParsedTransaction) -> tuple[str, int, str]:
    """Python FIFO 的稳定重放顺序：日期升序、同日 seq 降序、哈希升序。"""
    return (transaction.transaction_date, -transaction.sequence, transaction.transaction_hash)


def _display_symbol(symbol: str | None) -> str:
    """仅用于安全错误和告警显示，不改变账本中的原始值。"""
    return symbol if symbol is not None else "未知"

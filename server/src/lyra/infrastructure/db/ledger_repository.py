"""账本快照的 DuckDB 读取。

把 ``transactions`` / ``lots`` / ``realized`` 还原为领域对象，供分析层直接使用。

两条形状约定（都会静默影响结果，必须保持）：

1. **日期以 ISO 文本进出**。DuckDB 返回 ``date`` 对象，而领域层用文本排序与
   比较（``open_date``、``close_date``）；直接传对象会让排序退化成对象比较。
2. **账户以脱敏键呈现**。表里存的是 ``account_id`` UUID，领域层的账户身份是
   ``legacy_account_key``；少了这层 JOIN，所有账户范围过滤都会失效。

**用户范围是构造期绑定、不可绕过的基底**：``transactions`` / ``lots`` / ``realized``
按契约刻意不带 ``user_id``（P0 契约 §3.2），它们唯一的跨用户保护就是本模块每条查询
里的 ``a.user_id = ?``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
# 账本读取的排序键契约。
#
# **排序是分页正确性的一部分**：游标通过定位哈希在有序列表里的位置切页，顺序变了
# 就会返回不同的行，而且不报错。
#
# 三条都以唯一键收尾——没有唯一末键时数据库对并列行**不作顺序保证**，
# 同一查询两次可能给出不同顺序，分页会因此漏行或重复行。
LEDGER_READ_ORDERINGS: dict[str, tuple[str, ...]] = {
    "transactions": ("t.transaction_date", "t.sequence", "t.account_id", "t.transaction_hash"),
    "lots": ("l.account_id", "l.lot_id"),
    "realized": ("r.account_id", "r.realized_id"),
}

from decimal import Decimal
from typing import Any

import duckdb

from ...domain.analytics.types import AccountScope
from ...domain.fifo.types import Lot, RealizedRecord
from ...domain.ledger.types import ParsedTransaction


@dataclass(frozen=True)
class LedgerAccount:
    """账户的公开身份与基础币种。

    ``base_currency`` 是**按币种分组**的唯一依据：跨币种直接相加在会计上是错的，
    因此公开响应必须先按币种切分再合计。
    """

    id: str
    legacy_account_key: str
    base_currency: str


@dataclass(frozen=True)
class LedgerSnapshot:
    """分析所需的只读账本视图。"""

    accounts: tuple[LedgerAccount, ...]
    transactions: tuple[ParsedTransaction, ...]
    open_lots: tuple[Lot, ...]
    realized_records: tuple[RealizedRecord, ...]


class DuckDbLedgerRepository:
    """账本快照仓储。

    仓储在**构造时**绑定用户：账本的所有表（``transactions`` / ``lots`` /
    ``realized``）经 account 归属隔离，自身没有 ``user_id`` 列，跨用户保护**完全依赖
    本模块的查询范围**（P0 契约 §3.2）。把 ``user_id`` 做成方法参数会让每个调用点都能
    忘记传它；做成构造参数则每个实例天然只可能看到一个用户的账本。

    @param connection: 已迁移到当前 schema 的连接；本仓储只读。
    @param user_id: 当前用户的 UUID 文本；**必填**，不接受 ``None`` 表示全部用户。
    """

    def __init__(
        self, connection: duckdb.DuckDBPyConnection, *, user_id: str
    ) -> None:
        self._connection = connection
        self._user_id = user_id

    def load_snapshot(self, account_scope: AccountScope | None = None) -> LedgerSnapshot:
        """读取全部标准交易、未平批次与已实现明细。

        @param account_scope: 可选账户范围；``None`` 表示**该用户的**全部账户。
        @returns 按稳定顺序排列的三组领域对象。
        """
        scope = account_scope
        return LedgerSnapshot(
            accounts=self._accounts(scope),
            transactions=self._transactions(scope),
            open_lots=self._open_lots(scope),
            realized_records=self._realized(scope),
        )

    def _scope_filter(self, scope: AccountScope | None) -> tuple[str, list[Any]]:
        """构造账户范围过滤子句，**永远叠加当前用户条件**。

        ``scope.kind == "all"`` 以前不加任何条件，那在多用户下等于"返回全库账本"：
        A 的 ``scope=all`` 会算出 B 的持仓与已实现损益。因此这里把它改成
        「该用户的全部账户」——用户条件是**基底**，账户条件只是在它之上的收窄。

        账户键本身已在用户内唯一，仍与用户条件同时保留：账户键是用户输入（
        ``?scope=`` 最终会走到这里），只靠键匹配意味着任何人只要猜中别人的键就能读到
        别人的账本。

        @param scope: 账户范围；``None`` 或 ``kind == "all"`` 表示该用户的全部账户。
        @returns ``(SQL 片段, 参数)``；片段始终非空，参数以 ``user_id`` 开头。
        """
        where = "AND a.user_id = CAST(? AS UUID)"
        params: list[Any] = [self._user_id]
        if scope is not None and scope.kind != "all":
            where += " AND a.legacy_account_key = ?"
            params.append(scope.account)
        return where, params

    def _accounts(self, scope: AccountScope | None) -> tuple[LedgerAccount, ...]:
        """读取账户身份与基础币种，供公开响应按币种分组。"""
        where, params = self._scope_filter(scope)
        rows = self._connection.execute(
            f"""
            SELECT a.id, a.legacy_account_key, a.base_currency
            FROM accounts a
            WHERE a.status = 'active' {where}
            ORDER BY a.legacy_account_key
            """,
            params,
        ).fetchall()
        return tuple(
            LedgerAccount(id=str(row[0]), legacy_account_key=row[1], base_currency=row[2])
            for row in rows
        )

    def _transactions(self, scope: AccountScope | None) -> tuple[ParsedTransaction, ...]:
        """读取标准交易。"""
        where, params = self._scope_filter(scope)
        rows = self._connection.execute(
            f"""
            SELECT a.legacy_account_key, t.source_type, t.source_reference,
                   t.record_date, t.transaction_date, t.action, t.raw_symbol, t.description,
                   t.asset_type, t.underlying, t.expiry, t.strike, t.option_type,
                   t.quantity, t.price, t.fees, t.amount, t.source_hash, t.sequence,
                   t.occurrence, t.business_hash, t.transaction_hash
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE 1 = 1 {where}
            ORDER BY t.transaction_date, t.sequence, t.account_id, t.transaction_hash
            """,
            params,
        ).fetchall()
        return tuple(
            ParsedTransaction(
                account=row[0], source_type=row[1], source_reference=row[2],
                record_date=_date_text(row[3]), transaction_date=_date_text(row[4]),
                action=row[5], raw_symbol=row[6], description=row[7], asset_type=row[8],
                underlying=row[9], expiry=_date_text(row[10]) if row[10] is not None else None,
                strike=_decimal(row[11]), option_type=row[12],
                quantity=_decimal(row[13]), price=_decimal(row[14]), fees=_decimal(row[15]),
                amount=_decimal(row[16]), source_hash=row[17], sequence=int(row[18]),
                occurrence=int(row[19]), business_hash=row[20], transaction_hash=row[21],
            )
            for row in rows
        )

    def _open_lots(self, scope: AccountScope | None) -> tuple[Lot, ...]:
        """读取未平批次；完全平仓的批次不物化。"""
        where, params = self._scope_filter(scope)
        rows = self._connection.execute(
            f"""
            SELECT a.legacy_account_key, l.symbol_key, l.asset_type, l.underlying, l.expiry,
                   l.strike, l.option_type, l.direction, l.open_date, l.open_price,
                   l.open_quantity, l.remaining_quantity, l.open_fees, l.fees_allocated,
                   l.open_transaction_hash
            FROM lots l
            JOIN accounts a ON a.id = l.account_id
            WHERE l.remaining_quantity > 0 {where}
            ORDER BY l.account_id, l.lot_id
            """,
            params,
        ).fetchall()
        return tuple(
            Lot(
                account=row[0], symbol_key=row[1], asset_type=row[2], underlying=row[3],
                expiry=_date_text(row[4]) if row[4] is not None else None,
                strike=_decimal(row[5]), option_type=row[6], direction=row[7],
                open_date=_date_text(row[8]), open_price=_require_decimal(row[9]),
                open_quantity=_require_decimal(row[10]),
                remaining_quantity=_require_decimal(row[11]),
                open_fees=_require_decimal(row[12]),
                open_transaction=row[14],
                fees_allocated=_require_decimal(row[13]),
            )
            for row in rows
        )

    def _realized(self, scope: AccountScope | None) -> tuple[RealizedRecord, ...]:
        """读取已实现明细。"""
        where, params = self._scope_filter(scope)
        rows = self._connection.execute(
            f"""
            SELECT a.legacy_account_key, r.symbol_key, r.asset_type, r.underlying, r.direction,
                   r.open_date, r.close_date, r.quantity, r.open_price, r.close_price,
                   r.open_fees, r.close_fees, r.pnl, r.close_action,
                   r.open_transaction_hash, r.close_transaction_hash
            FROM realized r
            JOIN accounts a ON a.id = r.account_id
            WHERE 1 = 1 {where}
            ORDER BY r.account_id, r.realized_id
            """,
            params,
        ).fetchall()
        return tuple(
            RealizedRecord(
                account=row[0], symbol_key=row[1], asset_type=row[2], underlying=row[3],
                direction=row[4], open_date=_date_text(row[5]), close_date=_date_text(row[6]),
                quantity=_require_decimal(row[7]), open_price=_require_decimal(row[8]),
                close_price=_require_decimal(row[9]), open_fees=_require_decimal(row[10]),
                close_fees=_require_decimal(row[11]), pnl=_require_decimal(row[12]),
                close_action=row[13], open_transaction=row[14], close_transaction=row[15],
            )
            for row in rows
        )


def _decimal(value: Any) -> Decimal | None:
    """把可空数值列转为 Decimal。"""
    return None if value is None else Decimal(str(value))


def _require_decimal(value: Any) -> Decimal:
    """把非空数值列转为 Decimal。

    @raises ValueError: 列为空——非空列出现空值说明 schema 与代码已经不一致。
    """
    if value is None:
        raise ValueError("账本数值列不能为空")
    return Decimal(str(value))


def _date_text(value: Any) -> str:
    """把日期列渲染为 ISO 文本。

    领域层用**文本**排序与比较日期；返回 ``date`` 对象会让排序退化为对象比较，
    且跨类型比较会直接抛错。
    """
    if isinstance(value, datetime):
        return value.astimezone(UTC).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)

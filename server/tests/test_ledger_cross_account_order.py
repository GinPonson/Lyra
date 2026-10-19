"""跨账户排序：同一天、同一 sequence 的两笔交易。

**这条测试针对一个真实且常见的场景。**

``sequence`` 是**单个文件内的行号**（``sequence = index + 1``），因此两个不同账户的
文件在同一日期下**必然**都有 ``sequence = 1, 2, 3…``。

于是"同日同序不同账户"的并列行是**常态而非例外**，排序的第三键（``account_id``）会
决定它们的先后。参考实现用它，而此前我的实现直接跳到 ``transaction_hash``——
**两个不同的全序**，跨账户时必然分叉。

**M3 差分测试对这条分支是盲的**：夹具里每个用例只有一份 CSV（单账户），触发不了并列。
见 §172。
"""

from __future__ import annotations

import hashlib

from lyra.domain.analytics.types import AccountScope
from lyra.infrastructure.db.ledger_repository import DuckDbLedgerRepository


def _h(label: str) -> str:
    """稳定的 64 位十六进制摘要。"""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
ACCOUNT_B = "22222222-2222-2222-2222-222222222222"
USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"


def _seed_two_accounts(connection) -> None:
    """写入两个**同属一个用户**的账户，各一笔**同一天、同一 sequence** 的交易。

    两个账户同属一个用户是刻意的：跨账户排序只有在同一个用户范围内才有意义，若分属
    两个用户，用户范围会把它们分开、这条排序分支根本触发不到。

    关键在哈希的取值：账户 B 的交易哈希**排在账户 A 之前**（按字典序）。
    因此：

    - 若按 ``date, sequence, transaction_hash`` 排 → **B 在前**
    - 若按 ``date, sequence, account_id, transaction_hash`` 排 → **A 在前**

    两种顺序都能通过"是全序"的检查，只有对照参考实现的规则才能区分。
    """
    connection.execute(
        """
        INSERT INTO users (id, display_name, status, role)
        VALUES (CAST(? AS UUID), 'Order User', 'active', 'member')
        """,
        [USER_ID],
    )
    connection.execute(
        """
        INSERT INTO ledger_versions (version, user_id, reason, source_reference)
        VALUES (1, CAST(? AS UUID), 'file_import', 'src')
        """,
        [USER_ID],
    )
    for account_id, key, suffix in ((ACCOUNT_A, "TST001", "001"), (ACCOUNT_B, "TST002", "002")):
        connection.execute(
            """
            INSERT INTO accounts (id, user_id, display_name, legacy_account_key, masked_suffix)
            VALUES (CAST(? AS UUID), CAST(? AS UUID), ?, ?, ?)
            """,
            [account_id, USER_ID, f"Account {suffix}", key, suffix],
        )
    # 账户 B 的哈希故意排在账户 A 之前。
    rows = [
        (ACCOUNT_A, "zzzz" + "a" * 60),
        (ACCOUNT_B, "aaaa" + "a" * 60),
    ]
    for account_id, txn_hash in rows:
        connection.execute(
            """
            INSERT INTO transactions
                (account_id, record_date, transaction_date, action, raw_symbol, description,
                 asset_type, underlying, quantity, price, amount, source_type, source_reference,
                 source_hash, sequence, occurrence, business_hash, transaction_hash,
                 parser_version, ledger_version)
            VALUES (CAST(? AS UUID), CAST('2026-01-05' AS DATE), CAST('2026-01-05' AS DATE),
                    'Buy', 'INTC', 'INTEL CORP', 'stock', 'INTC',
                    CAST('10' AS DECIMAL(18,6)), CAST('40' AS DECIMAL(18,6)),
                    CAST('-400' AS DECIMAL(18,4)), 'csv', 'src', ?, 1, 1, ?, ?, 2, 1)
            """,
            [account_id, _h("source"), _h(f"business-{account_id}"), txn_hash],
        )


def test_same_day_same_sequence_rows_are_ordered_by_account(tmp_path) -> None:
    """**同日同序的两笔交易按账户键排**，而不是先按交易哈希排。

    这是参考实现的排序键（``date, sequence, account_id, transaction_hash``）产生的
    顺序；少了 ``account_id`` 就会颠倒——**而颠倒不会报错**。
    """
    from lyra.db.connection import open_lyra_database

    database = open_lyra_database(str(tmp_path / "order.duckdb"))
    try:
        _seed_two_accounts(database.connection)
        snapshot = DuckDbLedgerRepository(database.connection, user_id=USER_ID).load_snapshot(
            AccountScope(kind="all")
        )

        assert len(snapshot.transactions) == 2
        # 两笔属于不同账户、同一天、同一 sequence——唯一能区分的是 account_id。
        assert [t.account for t in snapshot.transactions] == ["TST001", "TST002"]
    finally:
        database.close()


def test_the_hash_order_really_would_have_differed(tmp_path) -> None:
    """**确认这条测试真的能区分两种排序**，而不是碰巧通过。

    若两笔交易的哈希顺序恰好与账户顺序一致，上一条测试就无法区分"有 account_id"
    与"没有 account_id"。这里显式断言哈希顺序与账户顺序**相反**——
    它让上一条测试成为一个有判别力的测试。
    """
    from lyra.db.connection import open_lyra_database

    database = open_lyra_database(str(tmp_path / "order2.duckdb"))
    try:
        _seed_two_accounts(database.connection)
        rows = database.connection.execute(
            """
            SELECT a.legacy_account_key, t.transaction_hash
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            """
        ).fetchall()
        by_hash = [key for key, _ in sorted(rows, key=lambda r: r[1])]

        assert by_hash == ["TST002", "TST001"], (
            "哈希顺序应与账户顺序相反，否则本文件的测试失去判别力"
        )
    finally:
        database.close()

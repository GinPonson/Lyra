"""账本读取的排序键契约。

**排序是分页正确性的一部分**：游标通过"定位哈希在有序列表里的位置"来切页。若顺序与
参考实现不同，**同一页请求会返回不同的行**——而且不报错。

三处读取各有一条排序键，此前都**少了 ``account_id``**：

| 表 | 参考实现 | 改前 |
| --- | --- | --- |
| transactions | ``date, sequence, account_id, hash`` | ``date, sequence, hash`` |
| lots | ``account_id, lot_id`` | ``lot_id`` |
| realized | ``account_id, realized_id`` | ``realized_id`` |

后两处尤其隐蔽：``lot_id`` / ``realized_id`` 是**全局序列**，因此单键排序本身是一个
**全序**——它不会报错也不会乱序，只是与参考实现的顺序**不同**。不同账户的批次交错时
两者就会分叉。
"""

from __future__ import annotations

from pathlib import Path

from lyra.infrastructure.db.ledger_repository import LEDGER_READ_ORDERINGS


def test_transaction_ordering_includes_the_account_id() -> None:
    """交易顺序含 ``account_id``，且位于 ``sequence`` 与 ``transaction_hash`` 之间。

    **位置也重要**：把它放到末尾会让"同日同序不同账户"的两笔交易按哈希排，
    而参考实现按账户排。
    """
    assert LEDGER_READ_ORDERINGS["transactions"] == (
        "t.transaction_date", "t.sequence", "t.account_id", "t.transaction_hash"
    )


def test_lot_ordering_is_account_first() -> None:
    """批次顺序**以账户开头**。

    ``lot_id`` 全局唯一，因此单键排序看起来"够用"——但它与
    ``(account_id, lot_id)`` 是不同的顺序，跨账户时必然分叉。
    """
    assert LEDGER_READ_ORDERINGS["lots"] == ("l.account_id", "l.lot_id")


def test_realized_ordering_is_account_first() -> None:
    """已实现明细顺序同样以账户开头。"""
    assert LEDGER_READ_ORDERINGS["realized"] == ("r.account_id", "r.realized_id")


def test_every_ordering_is_a_total_order() -> None:
    """**每条排序键都必须构成全序**（末键唯一）。

    没有末位唯一键时，数据库对并列行的顺序**不作保证**——同一查询两次可能给出不同
    顺序，而分页会因此漏行或重复行。
    """
    unique_tail = {
        "transactions": "t.transaction_hash",
        "lots": "l.lot_id",
        "realized": "r.realized_id",
    }
    for name, ordering in LEDGER_READ_ORDERINGS.items():
        assert ordering[-1] == unique_tail[name], (
            f"{name} 的末键不是唯一键，排序不是全序"
        )


def test_the_recorded_orderings_are_what_the_repository_uses() -> None:
    """**契约表必须就是仓储实际使用的键。**

    这条测试防止"改了 SQL 但忘了改契约表"——那样契约表会变成一份过时的文档，
    比没有更危险。
    """
    source = (
        Path(__file__).parent.parent / "src/lyra/infrastructure/db/ledger_repository.py"
    ).read_text(encoding="utf-8")

    for ordering in LEDGER_READ_ORDERINGS.values():
        assert "ORDER BY " + ", ".join(ordering) in source, (
            f"仓储里找不到 ORDER BY {', '.join(ordering)}"
        )

"""导入提交计划测试。

导入是唯一的写入入口，因此这些性质必须钉死：

1. **按交易哈希去重**——同一份文件重复导入或两次导出区间重叠时，已存在的交易
   被跳过而不是重复计入账本；
2. **无新增交易时不做 FIFO 重放**——重放整本账本是有成本的；
3. **账户范围失效时直接报错**——包括别的账户的交易意味着继续下去会污染本账户。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lyra.application.import_commit import (
    ImportCommitPreview,
    build_import_commit_plan,
)
from lyra.domain.ledger.types import ParsedTransaction

ACCOUNT = "TST001"


def _txn(
    txn_hash: str,
    *,
    account: str = ACCOUNT,
    action: str = "Buy",
    date: str = "2026-01-05",
    quantity: str = "10",
    price: str = "40",
    fees: str = "0",
    amount: str = "-400",
    asset_type: str = "stock",
) -> ParsedTransaction:
    """构造一条最小可用的标准交易。"""
    return ParsedTransaction(
        account=account, source_type="csv", source_reference="src",
        record_date=date, transaction_date=date, action=action,
        raw_symbol="INTC", description="INTEL CORP", asset_type=asset_type,
        underlying="INTC", expiry=None, strike=None, option_type=None,
        quantity=Decimal(quantity), price=Decimal(price), fees=Decimal(fees),
        amount=Decimal(amount), source_hash="s" * 64, sequence=1, occurrence=1,
        business_hash=txn_hash, transaction_hash=txn_hash,
    )


def _preview(*transactions: ParsedTransaction, account: str = ACCOUNT) -> ImportCommitPreview:
    """构造导入预览。"""
    return ImportCommitPreview(account=account, transactions=transactions)


def test_inserts_all_new_transactions() -> None:
    """全新交易应全部插入并触发 FIFO 重放。"""
    plan = build_import_commit_plan(_preview(_txn("h1"), _txn("h2")), ())

    assert plan.changed is True
    assert plan.inserted_rows == 2
    assert plan.skipped_rows == 0
    assert plan.fifo is not None
    assert plan.account == ACCOUNT
    assert [t.transaction_hash for t in plan.account_transactions] == ["h1", "h2"]


def test_skips_already_present_hashes() -> None:
    """已存在的交易必须被跳过，且不影响新增部分。"""
    existing = (_txn("h1"),)
    plan = build_import_commit_plan(_preview(_txn("h1"), _txn("h2")), existing)

    assert plan.changed is True
    assert plan.inserted_rows == 1
    assert plan.skipped_rows == 1
    assert [t.transaction_hash for t in plan.inserted_transactions] == ["h2"]


def test_reimport_of_same_file_is_a_no_op() -> None:
    """整份文件都是重复内容时，必须是无变更且**不做 FIFO 重放**。"""
    transactions = (_txn("h1"), _txn("h2"))
    plan = build_import_commit_plan(_preview(*transactions), transactions)

    assert plan.changed is False
    assert plan.inserted_rows == 0
    assert plan.skipped_rows == 2
    assert plan.fifo is None, "无新增交易却做了 FIFO 重放"
    # 提交后快照保持不变，调用方不必重写 lots/realized。
    assert plan.account_transactions == transactions


def test_preview_internal_duplicates_are_not_collapsed() -> None:
    """预览内部**不做**去重，只与既有账本比对（与现有实现一致）。

    `transaction_hash` 已包含 occurrence，因此解析同一份文件不会产出两条哈希相同的
    交易；真出现这种情况说明上游有 bug，应由数据库的唯一约束拒绝，而不是在这里
    静默吞掉一条。
    """
    plan = build_import_commit_plan(_preview(_txn("h1"), _txn("h1")), ())

    assert plan.inserted_rows == 2
    assert plan.skipped_rows == 0


def test_account_transactions_preserve_existing_then_append() -> None:
    """提交后快照是「既有 + 新增」，顺序稳定，便于与仓储写回对照。"""
    existing = (_txn("h1", date="2026-01-05"),)
    plan = build_import_commit_plan(_preview(_txn("h2", date="2026-01-06")), existing)

    assert plan.account_transactions[0].transaction_hash == "h1"
    assert plan.account_transactions[1].transaction_hash == "h2"


def test_rejects_transactions_from_another_account() -> None:
    """仓储返回别的账户的交易必须报错，而不是照单收下。"""
    existing = (_txn("h1", account="OTHER"),)

    with pytest.raises(ValueError, match="目标账户之外的交易"):
        build_import_commit_plan(_preview(_txn("h2")), existing)


def test_empty_preview_is_a_no_op() -> None:
    """空预览不产生变更，也不做重放。"""
    plan = build_import_commit_plan(_preview(), ())

    assert plan.changed is False
    assert plan.fifo is None
    assert plan.inserted_rows == 0


def test_fifo_replay_covers_the_whole_account_ledger() -> None:
    """重放必须覆盖既有 + 新增的**完整**账户账本，而不是只放新增部分。

    只重放新增部分会算错未平批次：旧的开仓批次会被当作不存在。
    """
    existing = (_txn("h-open", action="Buy", quantity="10", price="40", amount="-400"),)
    plan = build_import_commit_plan(
        _preview(_txn("h-close", action="Sell", date="2026-01-10", quantity="10",
                     price="60", amount="600")),
        existing,
    )

    assert plan.fifo is not None
    # 完整重放后该批次已平仓，因此没有未平批次、且产生一条已实现记录。
    assert plan.fifo.open_lots == ()
    assert len(plan.fifo.realized_records) == 1


def test_positive_fields_survive_the_plan_unchanged() -> None:
    """计划不改写交易内容，只决定取舍。"""
    original = _txn("h1", price="12.34", fees="0.66")
    plan = build_import_commit_plan(_preview(original), ())

    assert plan.inserted_transactions[0].price == Decimal("12.34")
    assert plan.inserted_transactions[0].fees == Decimal("0.66")
    assert plan.inserted_transactions[0].transaction_hash == original.transaction_hash

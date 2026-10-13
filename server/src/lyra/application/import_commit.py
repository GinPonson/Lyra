"""导入提交计划。

镜像 TypeScript 版 ``import-commit.ts``。

本模块在**打开写事务之前**完成去重与 FIFO 重放：写事务应当尽可能短，把可以
预先算完的东西留在外面。数据库适配器进入单写队列后仍需再次校验账本版本，
然后在一个事务中插入交易并替换该账户的 lots / realized。

一处刻意的优化：**无新增交易时不做 FIFO 重放**（``fifo`` 为 ``None``）。
重放整个账户账本是有成本的，重复导入同一份文件时完全没必要。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.fifo.replay import FifoReplayResult, replay_fifo
from ..domain.ledger.types import ParsedTransaction


@dataclass(frozen=True)
class ImportCommitPreview:
    """已通过提交快照校验的导入预览。"""

    account: str
    transactions: tuple[ParsedTransaction, ...]


@dataclass(frozen=True)
class ImportCommitPlan:
    """数据库事务开始前已经完成计算的导入变更计划。"""

    account: str
    changed: bool
    inserted_rows: int
    skipped_rows: int
    # 仅包含本次需要插入标准事实表的交易。
    inserted_transactions: tuple[ParsedTransaction, ...]
    # 受影响账户提交后的完整交易快照。
    account_transactions: tuple[ParsedTransaction, ...]
    # 有新增交易时的完整 FIFO 产物；无变更时为 ``None``。
    fifo: FifoReplayResult | None


def build_import_commit_plan(
    preview: ImportCommitPreview,
    existing_account_transactions: tuple[ParsedTransaction, ...],
) -> ImportCommitPlan:
    """根据当前账户事实表与已确认预览生成原子提交计划。

    去重依据是 ``transaction_hash``：同一份文件重复导入、或两次导出有重叠区间时，
    已存在的交易会被跳过而不是重复计入账本。

    @param preview: 已通过提交快照校验的导入预览。
    @param existing_account_transactions: 当前目标账户的完整标准交易快照。
    @returns 可直接交给 DuckDB 适配器执行的确定性计划。
    @raises ValueError: 仓储返回了目标账户之外的交易——说明账户范围过滤失效，
        继续下去会把别的账户的交易写进本账户。
    """
    foreign = next(
        (t for t in existing_account_transactions if t.account != preview.account), None
    )
    if foreign is not None:
        raise ValueError(
            f"仓储返回了目标账户之外的交易: expected={preview.account} actual={foreign.account}"
        )

    existing_hashes = {t.transaction_hash for t in existing_account_transactions}
    inserted = tuple(
        t for t in preview.transactions if t.transaction_hash not in existing_hashes
    )
    if not inserted:
        return ImportCommitPlan(
            account=preview.account,
            changed=False,
            inserted_rows=0,
            skipped_rows=len(preview.transactions),
            inserted_transactions=(),
            account_transactions=existing_account_transactions,
            fifo=None,
        )

    account_transactions = (*existing_account_transactions, *inserted)
    return ImportCommitPlan(
        account=preview.account,
        changed=True,
        inserted_rows=len(inserted),
        skipped_rows=len(preview.transactions) - len(inserted),
        inserted_transactions=inserted,
        account_transactions=account_transactions,
        fifo=replay_fifo(account_transactions),
    )

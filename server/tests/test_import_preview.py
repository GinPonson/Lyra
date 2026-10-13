"""导入预览与提交快照校验测试。

导入是两阶段的（预览 → 确认 → 提交），两阶段之间账本可能被其它导入改动，因此
预览必须携带指纹、提交时逐项复核。本文件钉住三条性质：

1. **指纹绑定四项条件**——文件、解析器版本、账户、基准账本版本，任一变化都必须
   使旧预览失效；
2. **一次性列出全部变化字段**——只报第一个会让用户在重新预览时反复撞冲突；
3. **已存在交易计入跳过数**，预览给出的"将插入 N 行"必须与实际提交一致。
"""

from __future__ import annotations

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_preview import (
    CreateImportPreviewInput,
    ImportCommitSnapshot,
    assert_import_preview_current,
    create_import_preview,
)
from lyra.domain.ledger.batch import TransactionBatchInput
from lyra.domain.ledger.types import RawTransactionFields

ACCOUNT = "TST001"


def _row(
    date: str = "01/05/2026",
    action: str = "Buy",
    symbol: str = "INTC",
    quantity: str = "10",
    price: str = "$40.00",
    fees: str = "",
    amount: str = "-$400.00",
) -> RawTransactionFields:
    """构造一行 Schwab 八列原始数据。"""
    return RawTransactionFields(
        date=date, action=action, symbol=symbol, description="INTEL CORP",
        quantity=quantity, price=price, fees=fees, amount=amount,
    )


def _batch(*rows: RawTransactionFields, source_hash: str = "f" * 64) -> TransactionBatchInput:
    """构造批次输入。"""
    return TransactionBatchInput(
        file_name="ledger.csv", account=ACCOUNT, source_hash=source_hash,
        source_type="csv", source_reference="ledger.csv", rows=rows,
    )


def _preview(
    *rows: RawTransactionFields,
    parser_version: str = "2",
    base_ledger_version: int = 0,
    existing: frozenset[str] = frozenset(),
    source_hash: str = "f" * 64,
):
    """生成预览。"""
    return create_import_preview(CreateImportPreviewInput(
        batch=_batch(*rows, source_hash=source_hash),
        parser_version=parser_version,
        base_ledger_version=base_ledger_version,
        existing_transaction_hashes=existing,
    ))


# --------------------------------------------------------------------------
# 预览内容
# --------------------------------------------------------------------------


def test_counts_insertable_rows_and_dates() -> None:
    """全新批次应全部可插入，并给出正确日期范围。"""
    preview = _preview(
        _row(date="01/05/2026", amount="-$400.00"),
        _row(date="01/10/2026", action="Sell", price="$60.00", amount="$600.00"),
    )

    assert preview.total_rows == 2
    assert preview.insertable_rows == 2
    assert preview.existing_rows == 0
    assert preview.first_transaction_date == "2026-01-05"
    assert preview.last_transaction_date == "2026-01-10"
    assert preview.account == ACCOUNT
    assert preview.file_name == "ledger.csv"


def test_existing_transactions_are_counted_as_skipped() -> None:
    """已存在的交易计入跳过数，插入数相应减少。"""
    first = _preview(_row())
    existing = frozenset(t.transaction_hash for t in first.transactions)

    again = _preview(_row(), existing=existing)

    assert again.total_rows == 1
    assert again.existing_rows == 1
    assert again.insertable_rows == 0


def test_by_action_is_grouped() -> None:
    """按 Action 统计条数，便于用户在预览页核对。"""
    preview = _preview(
        _row(action="Buy", amount="-$400.00"),
        _row(action="Buy", date="01/06/2026", quantity="5", amount="-$200.00"),
        _row(action="Sell", date="01/10/2026", price="$60.00", amount="$600.00"),
    )

    assert preview.by_action == {"Buy": 2, "Sell": 1}


@pytest.mark.parametrize("version", [-1, 1.5, True])
def test_rejects_invalid_base_ledger_version(version: object) -> None:
    """基准账本版本必须是非负整数。"""
    with pytest.raises(ValueError, match="非负安全整数"):
        _preview(_row(), base_ledger_version=version)  # type: ignore[arg-type]


@pytest.mark.parametrize("parser_version", ["", "   "])
def test_rejects_empty_parser_version(parser_version: str) -> None:
    """解析器版本不能为空——它是预览绑定条件之一。"""
    with pytest.raises(ValueError, match="不能为空"):
        _preview(_row(), parser_version=parser_version)


# --------------------------------------------------------------------------
# 指纹绑定
# --------------------------------------------------------------------------


def test_fingerprint_is_stable_for_identical_input() -> None:
    """同一输入必须得到同一指纹。"""
    assert _preview(_row()).preview_fingerprint == _preview(_row()).preview_fingerprint


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("file_hash", {"source_hash": "a" * 64}),
        ("parser_version", {"parser_version": "3"}),
        ("base_ledger_version", {"base_ledger_version": 7}),
    ],
)
def test_fingerprint_changes_when_a_bound_condition_changes(field: str, kwargs: dict) -> None:
    """文件、解析器版本、账本版本任一变化都必须改变指纹。"""
    baseline = _preview(_row()).preview_fingerprint
    changed = _preview(_row(), **kwargs).preview_fingerprint

    assert baseline != changed, f"{field} 未参与指纹绑定"


def test_fingerprint_changes_when_content_changes() -> None:
    """交易内容不同则指纹不同。"""
    baseline = _preview(_row()).preview_fingerprint
    changed = _preview(_row(price="$41.00", amount="-$410.00")).preview_fingerprint

    assert baseline != changed


def test_fingerprint_length_is_sha256_hex() -> None:
    """指纹是 64 位十六进制。"""
    assert len(_preview(_row()).preview_fingerprint) == 64


# --------------------------------------------------------------------------
# 提交快照校验
# --------------------------------------------------------------------------


def _snapshot(preview, **overrides) -> ImportCommitSnapshot:
    """按预览构造「当前条件」，可覆写任意一项。"""
    base = {
        "file_hash": preview.file_hash,
        "parser_version": preview.parser_version,
        "account": preview.account,
        "ledger_version": preview.base_ledger_version,
    }
    base.update(overrides)
    return ImportCommitSnapshot(**base)


def test_unchanged_conditions_pass() -> None:
    """条件完全一致时放行。"""
    preview = _preview(_row())
    assert_import_preview_current(preview, _snapshot(preview))


def test_stale_preview_reports_every_changed_field() -> None:
    """**一次性列出全部变化字段**，而不是只报第一个。

    只报一个会让用户在重新预览时反复撞上新的冲突。
    """
    preview = _preview(_row())

    with pytest.raises(ApplicationError) as raised:
        assert_import_preview_current(
            preview,
            _snapshot(preview, file_hash="a" * 64, ledger_version=9, parser_version="3"),
        )

    assert raised.value.code == "IMPORT_PREVIEW_STALE"
    assert raised.value.status_code == 409
    assert raised.value.details == {
        "changed_fields": ["file_hash", "parser_version", "ledger_version"]
    }


def test_ledger_version_bump_invalidates_preview() -> None:
    """账本被别的导入推进后，旧预览必须失效。"""
    preview = _preview(_row())

    with pytest.raises(ApplicationError) as raised:
        assert_import_preview_current(preview, _snapshot(preview, ledger_version=1))

    assert raised.value.details == {"changed_fields": ["ledger_version"]}


def test_account_change_invalidates_preview() -> None:
    """账户变化必须失效——否则会把交易提交到另一个账户。"""
    preview = _preview(_row())

    with pytest.raises(ApplicationError) as raised:
        assert_import_preview_current(preview, _snapshot(preview, account="OTHER"))

    assert raised.value.details == {"changed_fields": ["account"]}

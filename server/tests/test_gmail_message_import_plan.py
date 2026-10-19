"""Gmail 导入计划测试。

**决策顺序本身就是正确性的一部分**：

```
1. 已有记录且内容或账户归属变了 → 冲突（409）
2. 已有记录且已导入             → 跳过
3. 其余                         → 就绪
```

冲突必须在"已导入"之前：一封已导入的邮件若内容变了，先报"已导入"会让**变更被静默
忽略**，而用户以为这封邮件已经被处理过了。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from lyra.application.app_error import ApplicationError
from lyra.domain.gmail.econfirm import (
    EmailTrade,
    GmailMessage,
    UnresolvedCashOptionSaleError,
)
from lyra.domain.ledger.batch import TransactionBatchInput, normalize_transaction_batch
from lyra.domain.ledger.types import RawTransactionFields
from lyra.infrastructure.gmail.message_import_plan import (
    AlreadyImportedGmailMessagePlan,
    ExistingGmailMessageState,
    ReadyGmailMessageImportPlan,
    build_gmail_message_import_plan,
    plan_summary,
    _align_existing_transaction_facts,
)

NOW = datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC)
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def _message(
    body: str = "Date: 01/05/2026\nAction: Buy", *, message_id: str = "m1"
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id, sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms", body=body,
        email_timestamp="2026-01-05T10:00:00.000Z",
    )


def _trades(count: int = 1) -> tuple[EmailTrade, ...]:
    return tuple(
        EmailTrade(
            trade_date="2026-01-05", action="Buy", symbol=f"SYM{index}",
            description="X", quantity="10", price="40.5", fees="0", amount="-405",
        )
        for index in range(count)
    )


def _plan(
    *, message=None, trades=None, existing=None, existing_transactions=(),
    existing_maxima=None,
):
    return build_gmail_message_import_plan(
        connection_id="c1", thread_id=None, message=message or _message(),
        trades=trades if trades is not None else _trades(),
        account_id=ACCOUNT_ID, ledger_account="TST001", history_id=None,
        synced_at=NOW, parser_version="2", base_ledger_version=0,
        existing_message=existing,
        existing_account_transactions=tuple(existing_transactions),
        existing_gmail_occurrence_maxima=existing_maxima,
    )


def _hash_of(message: GmailMessage) -> str:
    from lyra.infrastructure.gmail.message_metadata import (
        create_parsed_gmail_message_metadata,
    )

    return create_parsed_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=message, trade_count=1,
        account_id=ACCOUNT_ID, history_id=None, synced_at=NOW,
    ).content_hash


def _option_trade() -> EmailTrade:
    """Create a synthetic, fully specified option execution for import-plan tests."""
    return EmailTrade(
        trade_date="2026-01-05", action="Buy to Open",
        symbol="SYNTHX 01/16/2026 100 C",
        description="CALL SYNTHETIC ISSUER $100 EXP 01/16/26",
        quantity="1", price="2.00", fees="0.65", amount="-200.65",
    )


def _csv_option_rows(trade: EmailTrade, *, descriptions: tuple[str, ...] | None = None):
    """Normalize synthetic CSV rows with descriptions that differ from eConfirm text."""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    fields = email_trade_to_raw_fields(trade)
    csv_descriptions = descriptions or (
        "SYNTHETIC ISSUER 01/16/2026 $100.00 Call",
    )
    return normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv", account="TST001", source_hash="csv-source",
        source_type="csv", source_reference="transactions.csv",
        rows=tuple(replace(fields, description=description) for description in csv_descriptions),
    )).transactions


def _stock_trade(
    *,
    trade_date: str = "2026-01-05",
    action: str = "Buy",
    symbol: str = "BRK.B",
    description: str = "BERKSHIRE HATHAWAY B",
    quantity: str = "2",
    price: str = "10.00",
    fees: str = "0.01",
    amount: str = "-20.01",
) -> EmailTrade:
    """Create a synthetic stock execution with the confirmed alias description shape."""
    return EmailTrade(
        trade_date=trade_date, action=action, symbol=symbol,
        description=description, quantity=quantity, price=price,
        fees=fees, amount=amount,
    )


def _csv_stock_rows(
    trade: EmailTrade,
    *,
    symbol: str | None = None,
    description: str | None = None,
    date: str | None = None,
    count: int = 1,
):
    """Normalize synthetic CSV facts with independently selected security metadata."""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    fields = email_trade_to_raw_fields(trade)
    fields = replace(
        fields,
        symbol=fields.symbol if symbol is None else symbol,
        description=fields.description if description is None else description,
        date=fields.date if date is None else date,
    )
    return normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv", account="TST001", source_hash="csv-source",
        source_type="csv", source_reference="transactions.csv",
        rows=(fields,) * count,
    )).transactions


# --------------------------------------------------------------------------
# 首次导入
# --------------------------------------------------------------------------


def test_first_import_is_ready() -> None:
    """没有已有记录时就绪。"""
    plan = _plan()

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.kind == "ready"
    assert plan.metadata.parse_status == "imported"
    assert plan.commit.inserted_rows == 1
    assert plan.metadata.transaction_count == 1


def _confirmed_cash_option_sale() -> EmailTrade:
    """构造仅可关联已有成交的合成 Cash 期权卖出，不提供新增授权。"""
    return replace(
        _option_trade(), action="Sell to Open", amount="199.35", existing_only=True
    )


def test_cash_option_sale_without_existing_execution_cannot_insert() -> None:
    """即使调用者提供了动作，Cash 的只关联标记仍阻止凭空新增交易。"""
    with pytest.raises(UnresolvedCashOptionSaleError, match="没有足够"):
        _plan(trades=(_confirmed_cash_option_sale(),))


def test_cash_option_sale_links_confirmed_csv_without_inserting() -> None:
    """描述展示不同但成交事实精确相同的 Cash 行关联原 CSV 哈希。"""
    trade = _confirmed_cash_option_sale()
    existing = _csv_option_rows(trade)
    plan = _plan(trades=(trade,), existing_transactions=existing)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 1
    assert plan.links[0].transaction_hash == existing[0].transaction_hash


def test_cash_option_sales_preserve_each_confirmed_occurrence() -> None:
    """已有两笔相同成交时分别关联，不能把真实重复成交合并成一笔。"""
    trade = _confirmed_cash_option_sale()
    existing = _csv_option_rows(trade, descriptions=("CSV CALL", "CSV CALL"))
    plan = _plan(trades=(trade, trade), existing_transactions=existing)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 2
    assert {link.transaction_hash for link in plan.links} == {
        transaction.transaction_hash for transaction in existing
    }


@pytest.mark.parametrize("already_linked", [0, 1])
def test_cash_option_sale_cannot_exceed_confirmed_occurrences(
    already_linked: int,
) -> None:
    """本封和跨邮件的重复出现都不得超过已有可证明的成交次数。"""
    trade = _confirmed_cash_option_sale()
    existing = _csv_option_rows(trade)
    trades = (trade, trade) if already_linked == 0 else (trade,)
    with pytest.raises(UnresolvedCashOptionSaleError, match="没有足够"):
        _plan(
            trades=trades,
            existing_transactions=existing,
            existing_maxima={existing[0].business_hash: already_linked},
        )


def test_cash_existing_only_flag_does_not_block_other_proven_new_trades() -> None:
    """同封中已知动作的新成交仍可新增，Cash 行只关联已有事实。"""
    trade = _confirmed_cash_option_sale()
    existing = _csv_option_rows(trade)
    plan = _plan(
        trades=(trade, *_trades()), existing_transactions=existing
    )
    assert plan.commit.inserted_rows == 1
    assert plan.commit.skipped_rows == 1


def test_explicit_option_sale_action_can_still_insert_normally() -> None:
    """邮件明确给出开仓动作的路径维持原有新增行为。"""
    trade = replace(_confirmed_cash_option_sale(), existing_only=False)
    plan = _plan(trades=(trade,))
    assert plan.commit.inserted_rows == 1


def test_links_are_one_based_and_ordered() -> None:
    """关联序号**从 1 开始**：0 会让"第一条"与"未设置"难以区分。"""
    plan = _plan(trades=_trades(3))

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert [link.ordinal for link in plan.links] == [1, 2, 3]
    assert [link.transaction_hash for link in plan.links] == [
        transaction.transaction_hash for transaction in plan.commit.inserted_transactions
    ]


def test_identical_rows_in_one_email_match_csv_occurrences_in_order() -> None:
    """一封多笔邮件按本封出现顺序对齐 CSV 的相同 occurrence。"""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    trade = _trades(1)[0]
    fields = email_trade_to_raw_fields(trade)
    csv_rows = normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv",
        account="TST001",
        source_hash="csv-source",
        source_type="csv",
        source_reference="transactions.csv",
        rows=(fields, fields),
    )).transactions

    plan = _plan(
        trades=(trade, trade),
        existing_transactions=csv_rows,
    )

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 2
    assert [link.transaction_hash for link in plan.links] == [
        transaction.transaction_hash for transaction in csv_rows
    ]


def test_option_description_variant_reuses_exact_existing_csv_business_hash() -> None:
    """只用其余原始成交字段完全相同的期权 CSV 行复用已有描述。"""
    trade = _option_trade()
    csv_rows = _csv_option_rows(trade)

    plan = _plan(trades=(trade,), existing_transactions=csv_rows)

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 1
    assert plan.links[0].transaction_hash == csv_rows[0].transaction_hash


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        # Numeric equality is insufficient: the raw decimal scale is part of the hash.
        ("price", "$2.000"),
        ("fees", "$0.650"),
        ("amount", "-$200.650"),
        # A numerically different net amount must not inherit the CSV description either.
        ("fees", "$0.66"),
    ],
)
def test_option_description_reuse_requires_exact_amount_and_precision(
    field_name: str, changed_value: str,
) -> None:
    """金额或小数位不同的候选不得被当成同一笔成交。"""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    trade = _option_trade()
    email_fields = email_trade_to_raw_fields(trade)
    changed_fields = replace(
        email_fields,
        description="SYNTHETIC ISSUER 01/16/2026 $100.00 Call",
        **{field_name: changed_value},
    )
    if field_name == "fees" and changed_value == "$0.66":
        changed_fields = replace(changed_fields, amount="-$200.66")
    existing = normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv", account="TST001", source_hash="csv-source",
        source_type="csv", source_reference="transactions.csv", rows=(changed_fields,),
    )).transactions

    aligned = _align_existing_transaction_facts(
        (email_fields,), existing, "TST001"
    )

    assert aligned[0].description == email_fields.description


def test_option_description_match_with_multiple_csv_hashes_is_a_conflict() -> None:
    """多个不同描述对应同一组其它字段时，不猜测应关联哪条 CSV 事实。"""
    trade = _option_trade()
    csv_rows = _csv_option_rows(
        trade,
        descriptions=(
            "SYNTHETIC ISSUER 01/16/2026 $100.00 Call",
            "SYNTHETIC ISSUER CALL EXP 01/16/26",
        ),
    )

    with pytest.raises(ApplicationError) as raised:
        _plan(trades=(trade,), existing_transactions=csv_rows)

    assert raised.value.code == "GMAIL_REPLAY_ORDER_AMBIGUOUS"
    assert raised.value.status_code == 409
    assert raised.value.details == {"reason": "transaction_fact_match_ambiguous"}


def test_option_description_reuse_continues_across_csv_occurrences_and_messages() -> None:
    """邮件间 occurrence 继续对齐 CSV 行，复用描述后不产生新交易。"""
    trade = _option_trade()
    csv_rows = _csv_option_rows(
        trade,
        descriptions=(
            "SYNTHETIC ISSUER 01/16/2026 $100.00 Call",
            "SYNTHETIC ISSUER 01/16/2026 $100.00 Call",
        ),
    )
    first = _plan(
        message=_message("option fill", message_id="m1"),
        trades=(trade,), existing_transactions=csv_rows,
    )
    second = _plan(
        message=_message("option fill", message_id="m2"),
        trades=(trade,), existing_transactions=csv_rows,
        existing_maxima={csv_rows[0].business_hash: 1},
    )

    assert isinstance(first, ReadyGmailMessageImportPlan)
    assert isinstance(second, ReadyGmailMessageImportPlan)
    assert first.commit.inserted_rows == second.commit.inserted_rows == 0
    assert first.commit.skipped_rows == second.commit.skipped_rows == 1
    assert first.links[0].transaction_hash == csv_rows[0].transaction_hash
    assert second.links[0].transaction_hash == csv_rows[1].transaction_hash


def test_stock_alias_matches_real_schwab_symbol_and_description_pair() -> None:
    """已确认的股票代码与描述组合重建原 CSV hash 并跳过，不新增账本行。"""
    trade = _stock_trade()
    csv_rows = _csv_stock_rows(
        trade,
        symbol="BRKB",
        description="BERKSHIRE HATHAWAY CLASS B",
    )

    plan = _plan(trades=(trade,), existing_transactions=csv_rows)

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 1
    assert plan.links[0].transaction_hash == csv_rows[0].transaction_hash


def test_stock_alias_class_name_variants_link_existing_csv_exactly() -> None:
    """Class B 与嘉信重复 CLASS 的明确表示仍须精确命中原有哈希。"""
    trade = _stock_trade(description="BERKSHIRE HATHAWAY Class B")
    existing = _csv_stock_rows(
        trade, symbol="BRKB", description="BERKSHIRE HATHAWAY CLASS CLASS B"
    )
    plan = _plan(trades=(trade,), existing_transactions=existing)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 1
    assert plan.links[0].transaction_hash == existing[0].transaction_hash


def test_stock_alias_class_variants_preserve_multiple_csv_occurrences() -> None:
    """名称差异不能合并同一邮件中真实存在的多次相同成交。"""
    trade = _stock_trade(description="BERKSHIRE HATHAWAY Class B")
    existing = _csv_stock_rows(
        trade, symbol="BRKB", description="BERKSHIRE HATHAWAY CLASS CLASS B", count=2
    )
    plan = _plan(trades=(trade, trade), existing_transactions=existing)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 2
    assert {link.transaction_hash for link in plan.links} == {
        transaction.transaction_hash for transaction in existing
    }


def test_stock_description_difference_reuses_only_exact_existing_trade_facts() -> None:
    """同一股票的不同描述只有在成交事实精确命中时才复用 CSV 描述。"""
    trade = _stock_trade(symbol="SYNTH", description="SYNTHETIC ISSUER")
    csv_rows = _csv_stock_rows(
        trade, description="SYNTHETIC ISSUER CLASS A"
    )

    plan = _plan(trades=(trade,), existing_transactions=csv_rows)

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 0
    assert plan.links[0].transaction_hash == csv_rows[0].transaction_hash


def test_stock_as_of_record_date_is_reused_after_exact_transaction_date_match() -> None:
    """邮件交易日与 CSV 实际成交日一致时复用 CSV 的 as-of 记录日期。"""
    trade = _stock_trade(symbol="SYNTH", description="EMAIL DESCRIPTION")
    csv_rows = _csv_stock_rows(
        trade,
        description="CSV DESCRIPTION",
        date="01/06/2026 as of 01/05/2026",
    )

    plan = _plan(trades=(trade,), existing_transactions=csv_rows)

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 1
    assert plan.links[0].transaction_hash == csv_rows[0].transaction_hash


def test_as_of_candidate_with_different_transaction_date_does_not_match() -> None:
    """仅记录日相近不能跨过实际成交日这一身份字段。"""
    trade = _stock_trade(symbol="SYNTH", description="EMAIL DESCRIPTION")
    csv_rows = _csv_stock_rows(
        trade,
        description="CSV DESCRIPTION",
        date="01/06/2026 as of 01/04/2026",
    )
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    email_fields = email_trade_to_raw_fields(trade)
    aligned = _align_existing_transaction_facts((email_fields,), csv_rows, "TST001")

    assert aligned[0] == email_fields


def test_stock_fact_reuse_rejects_numeric_text_precision_difference() -> None:
    """相同数值但原始费用精度不同也不能复用 CSV 业务哈希。"""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    trade = _stock_trade(symbol="SYNTH", description="EMAIL DESCRIPTION")
    fields = email_trade_to_raw_fields(trade)
    csv_fields = replace(
        fields,
        description="CSV DESCRIPTION",
        fees="$0.010",
    )
    existing = normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv", account="TST001", source_hash="csv-source",
        source_type="csv", source_reference="transactions.csv", rows=(csv_fields,),
    )).transactions

    aligned = _align_existing_transaction_facts((fields,), existing, "TST001")

    assert aligned[0] == fields


@pytest.mark.parametrize(
    ("symbol", "description"),
    [
        ("BRK.A", "BERKSHIRE HATHAWAY CLASS A"),
        ("BRK.B", "OTHER ISSUER CLASS B"),
    ],
)
def test_stock_alias_does_not_merge_other_share_class_or_issuer(
    symbol: str, description: str,
) -> None:
    """别名只包含已核实的 B 类发行人组合。"""
    existing_trade = _stock_trade(
        trade_date="2026-01-04", symbol="BRKB",
        description="BERKSHIRE HATHAWAY CLASS B",
    )
    existing = _csv_stock_rows(existing_trade)
    trade = _stock_trade(symbol=symbol, description=description)

    plan = _plan(trades=(trade,), existing_transactions=existing)

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 1
    assert plan.commit.inserted_transactions[0].raw_symbol == symbol
    assert plan.commit.inserted_transactions[0].description == description


def test_stock_alias_with_two_account_representations_is_a_conflict() -> None:
    """同一账户同时存在别名两种代码表示时不任意选择。"""
    trade = _stock_trade()
    csv_as_brkb = _csv_stock_rows(
        trade, symbol="BRKB", description="BERKSHIRE HATHAWAY CLASS B"
    )
    csv_as_brk_dot_b = _csv_stock_rows(trade, symbol="BRK.B")

    with pytest.raises(ApplicationError) as raised:
        _plan(
            trades=(trade,),
            existing_transactions=(*csv_as_brkb, *csv_as_brk_dot_b),
        )

    assert raised.value.code == "GMAIL_REPLAY_ORDER_AMBIGUOUS"
    assert raised.value.status_code == 409
    assert raised.value.details == {"reason": "stock_alias_representation_ambiguous"}


def test_new_stock_alias_event_uses_existing_fifo_symbol_without_reusing_old_description() -> None:
    """新增别名交易沿用账户代码进入同一 FIFO 持仓，且保留邮件描述。"""
    opening_trade = _stock_trade(
        trade_date="2026-01-04", symbol="BRKB",
        description="BERKSHIRE HATHAWAY CLASS B",
        quantity="2", price="10.00", fees="0", amount="-20.00",
    )
    existing = _csv_stock_rows(opening_trade)
    closing_trade = _stock_trade(
        trade_date="2026-01-05", action="Sell", symbol="BRK.B",
        description="BERKSHIRE HATHAWAY B",
        quantity="2", price="11.00", fees="0", amount="22.00",
    )

    plan = _plan(trades=(closing_trade,), existing_transactions=existing)

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 1
    inserted = plan.commit.inserted_transactions[0]
    assert inserted.raw_symbol == "BRKB"
    assert inserted.description == "BERKSHIRE HATHAWAY B"
    assert plan.commit.fifo is not None
    assert len(plan.commit.fifo.realized_records) == 1
    assert plan.commit.fifo.open_lots == ()


def test_stock_alias_keeps_distinct_existing_occurrences_across_messages() -> None:
    """已有重复成交保留 occurrence，两封邮件分别链接对应 CSV 行。"""
    trade = _stock_trade()
    csv_rows = _csv_stock_rows(
        trade,
        symbol="BRKB",
        description="BERKSHIRE HATHAWAY CLASS B",
        count=2,
    )
    first = _plan(message=_message("same body", message_id="alias-1"),
                  trades=(trade,), existing_transactions=csv_rows)
    second = _plan(
        message=_message("same body", message_id="alias-2"),
        trades=(trade,), existing_transactions=csv_rows,
        existing_maxima={csv_rows[0].business_hash: 1},
    )

    assert isinstance(first, ReadyGmailMessageImportPlan)
    assert isinstance(second, ReadyGmailMessageImportPlan)
    assert first.commit.inserted_rows == second.commit.inserted_rows == 0
    assert first.links[0].transaction_hash == csv_rows[0].transaction_hash
    assert second.links[0].transaction_hash == csv_rows[1].transaction_hash


def test_next_email_continues_occurrence_and_links_exact_csv_row() -> None:
    """已有 Gmail link 的最大 occurrence 作为跨邮件接续点，CSV 精确命中可跳过。"""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    trade = _trades(1)[0]
    fields = email_trade_to_raw_fields(trade)
    csv_rows = normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv",
        account="TST001",
        source_hash="csv-source",
        source_type="csv",
        source_reference="transactions.csv",
        rows=(fields, fields),
    )).transactions

    plan = build_gmail_message_import_plan(
        connection_id="c1", thread_id=None,
        message=_message("same body", message_id="m2"),
        trades=(trade,), account_id=ACCOUNT_ID, ledger_account="TST001",
        history_id=None, synced_at=NOW, parser_version="2", base_ledger_version=0,
        existing_message=None, existing_account_transactions=csv_rows,
        existing_gmail_occurrence_maxima={csv_rows[0].business_hash: 1},
    )

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.commit.inserted_rows == 0
    assert plan.commit.skipped_rows == 1
    assert plan.links[0].transaction_hash == csv_rows[1].transaction_hash


def test_identical_body_from_distinct_messages_is_distinct_fifo_source() -> None:
    """不同 message ID 即使正文相同也不能冒充一个可排序的来源快照。"""
    first = _plan(message=_message("same body", message_id="m1"))
    assert isinstance(first, ReadyGmailMessageImportPlan)
    existing = first.commit.account_transactions
    business_hash = first.commit.inserted_transactions[0].business_hash

    with pytest.raises(ApplicationError) as raised:
        build_gmail_message_import_plan(
            connection_id="c1", thread_id=None,
            message=_message("same body", message_id="m2"),
            trades=_trades(), account_id=ACCOUNT_ID, ledger_account="TST001",
            history_id=None, synced_at=NOW, parser_version="2", base_ledger_version=0,
            existing_message=None, existing_account_transactions=existing,
            existing_gmail_occurrence_maxima={business_hash: 1},
        )

    assert raised.value.code == "GMAIL_REPLAY_ORDER_AMBIGUOUS"


def test_identical_messages_link_distinct_csv_occurrences_without_inserting() -> None:
    """CSV 已有 occurrence 时，两封同正文邮件分别链接相应行并精确跳过。"""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    trade = _trades(1)[0]
    fields = email_trade_to_raw_fields(trade)
    csv_rows = normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv", account="TST001", source_hash="csv-source",
        source_type="csv", source_reference="transactions.csv", rows=(fields, fields),
    )).transactions
    first = build_gmail_message_import_plan(
        connection_id="c1", thread_id=None,
        message=_message("same body", message_id="m1"), trades=(trade,),
        account_id=ACCOUNT_ID, ledger_account="TST001", history_id=None,
        synced_at=NOW, parser_version="2", base_ledger_version=0,
        existing_message=None, existing_account_transactions=csv_rows,
    )
    second = build_gmail_message_import_plan(
        connection_id="c1", thread_id=None,
        message=_message("same body", message_id="m2"), trades=(trade,),
        account_id=ACCOUNT_ID, ledger_account="TST001", history_id=None,
        synced_at=NOW, parser_version="2", base_ledger_version=0,
        existing_message=None, existing_account_transactions=csv_rows,
        existing_gmail_occurrence_maxima={csv_rows[0].business_hash: 1},
    )

    assert isinstance(first, ReadyGmailMessageImportPlan)
    assert isinstance(second, ReadyGmailMessageImportPlan)
    assert first.commit.skipped_rows == second.commit.skipped_rows == 1
    assert first.links[0].transaction_hash == csv_rows[0].transaction_hash
    assert second.links[0].transaction_hash == csv_rows[1].transaction_hash


def test_new_same_day_nonidentical_source_order_is_a_stable_conflict() -> None:
    """FIFO 无法证明同日多来源顺序时，Gmail 边界返回稳定冲突码。"""
    from lyra.domain.gmail.econfirm import email_trade_to_raw_fields

    trade = _trades(1)[0]
    fields = email_trade_to_raw_fields(trade)
    existing = normalize_transaction_batch(TransactionBatchInput(
        file_name="transactions.csv",
        account="TST001",
        source_hash="csv-source",
        source_type="csv",
        source_reference="transactions.csv",
        rows=(RawTransactionFields(
            date=fields.date, action=fields.action, symbol=fields.symbol,
            description=fields.description, quantity="11", price=fields.price,
            fees=fields.fees, amount="-445.5",
        ),),
    )).transactions

    with pytest.raises(ApplicationError) as raised:
        _plan(trades=(trade,), existing_transactions=existing)

    assert raised.value.code == "GMAIL_REPLAY_ORDER_AMBIGUOUS"
    assert raised.value.message == "同日成交顺序无法确认，请导入包含该日全部成交的嘉信 CSV。"
    assert raised.value.status_code == 409


def test_links_carry_the_ledger_account_identity() -> None:
    """关联带账户 UUID 与消息身份，便于回溯。"""
    plan = _plan()

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    link = plan.links[0]
    assert link.connection_id == "c1"
    assert link.message_id == "m1"
    assert link.account_id == ACCOUNT_ID


def test_batch_name_and_source_reference_use_the_message_id() -> None:
    """批次名与来源引用都用消息 ID，便于诊断时定位到具体邮件。"""
    plan = _plan()

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert plan.metadata.message_id == "m1"


# --------------------------------------------------------------------------
# 已导入：跳过
# --------------------------------------------------------------------------


def test_unchanged_imported_message_is_skipped() -> None:
    """内容与账户归属都未变且已导入时跳过。"""
    message = _message()
    plan = _plan(
        message=message,
        existing=ExistingGmailMessageState(
            content_hash=_hash_of(message), account_id=ACCOUNT_ID,
            parse_status="imported",
        ),
    )

    assert isinstance(plan, AlreadyImportedGmailMessagePlan)
    assert plan.kind == "already_imported"
    assert plan.message_id == "m1"


@pytest.mark.parametrize("status", ["rejected", "conflict", "parsed", "discovered"])
def test_failed_records_are_retried(status: str) -> None:
    """**解析失败或冲突的记录会重新尝试。**

    那是修复后重试的路径；因为"库里有记录"就跳过会让用户永远无法重新导入。
    """
    message = _message()
    plan = _plan(
        message=message,
        existing=ExistingGmailMessageState(
            content_hash=_hash_of(message),
            account_id=None if status in ("rejected", "conflict") else ACCOUNT_ID,
            parse_status=status,  # type: ignore[arg-type]
        ),
    )

    assert isinstance(plan, ReadyGmailMessageImportPlan)


# --------------------------------------------------------------------------
# 冲突：内容或账户归属变化
# --------------------------------------------------------------------------


def test_changed_content_is_a_conflict() -> None:
    """内容变了必须报冲突，而不是当成"已导入"跳过。"""
    existing = ExistingGmailMessageState(
        content_hash="0" * 64, account_id=ACCOUNT_ID, parse_status="imported"
    )

    with pytest.raises(ApplicationError) as raised:
        _plan(existing=existing)

    assert raised.value.code == "GMAIL_MESSAGE_CONFLICT"
    assert raised.value.status_code == 409
    assert raised.value.details is not None
    assert raised.value.details["changed_fields"] == ["content_hash"]


def test_changed_account_is_a_conflict() -> None:
    """账户归属变了同样报冲突——同一封邮件不能悄无声息地改归属。"""
    message = _message()
    existing = ExistingGmailMessageState(
        content_hash=_hash_of(message), account_id="22222222-2222-2222-2222-222222222222",
        parse_status="imported",
    )

    with pytest.raises(ApplicationError) as raised:
        _plan(message=message, existing=existing)

    assert raised.value.details is not None
    assert raised.value.details["changed_fields"] == ["account_id"]


def test_both_changes_are_listed_in_a_stable_order() -> None:
    """两项都变时按 content_hash → account_id 的固定顺序列出。"""
    existing = ExistingGmailMessageState(
        content_hash="0" * 64, account_id="22222222-2222-2222-2222-222222222222",
        parse_status="imported",
    )

    with pytest.raises(ApplicationError) as raised:
        _plan(existing=existing)

    assert raised.value.details is not None
    assert raised.value.details["changed_fields"] == ["content_hash", "account_id"]


def test_conflict_message_reports_the_message_id() -> None:
    """冲突详情带消息 ID，便于定位。"""
    existing = ExistingGmailMessageState(
        content_hash="0" * 64, account_id=ACCOUNT_ID, parse_status="imported"
    )

    with pytest.raises(ApplicationError) as raised:
        _plan(existing=existing)

    assert raised.value.details is not None
    assert raised.value.details["message_id"] == "m1"


def test_conflict_is_checked_before_already_imported() -> None:
    """**顺序验证**：已导入但内容变了，报的是冲突而不是"已导入"。

    反过来会让变更被静默忽略，而用户以为这封邮件已经被处理过了。
    """
    existing = ExistingGmailMessageState(
        content_hash="0" * 64, account_id=ACCOUNT_ID, parse_status="imported"
    )

    with pytest.raises(ApplicationError) as raised:
        _plan(existing=existing)

    assert raised.value.code == "GMAIL_MESSAGE_CONFLICT"


# --------------------------------------------------------------------------
# 摘要输出
# --------------------------------------------------------------------------


def test_summary_never_contains_message_content() -> None:
    """**诊断摘要不含正文或成交明细**——日志常被转发到聚合系统。"""
    plan = _plan(message=_message(body="SECRET BODY CONTENT"))
    summary = plan_summary(plan)

    assert "SECRET" not in str(summary)
    assert summary["messageId"] == "m1"
    assert summary["insertedRows"] == 1


def test_summary_of_a_skipped_plan_is_minimal() -> None:
    """跳过的计划摘要只含身份与摘要。"""
    message = _message()
    plan = _plan(
        message=message,
        existing=ExistingGmailMessageState(
            content_hash=_hash_of(message), account_id=ACCOUNT_ID,
            parse_status="imported",
        ),
    )

    assert plan_summary(plan)["kind"] == "already_imported"

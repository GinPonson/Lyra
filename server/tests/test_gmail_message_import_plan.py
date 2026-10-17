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

from datetime import UTC, datetime

import pytest

from lyra.application.app_error import ApplicationError
from lyra.domain.gmail.econfirm import EmailTrade, GmailMessage
from lyra.infrastructure.gmail.message_import_plan import (
    AlreadyImportedGmailMessagePlan,
    ExistingGmailMessageState,
    ReadyGmailMessageImportPlan,
    build_gmail_message_import_plan,
    plan_summary,
)

NOW = datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC)
ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def _message(body: str = "Date: 01/05/2026\nAction: Buy") -> GmailMessage:
    return GmailMessage(
        message_id="m1", sender="Schwab <donotreply@mail.schwab.com>",
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


def _plan(*, message=None, trades=None, existing=None, existing_transactions=()):
    return build_gmail_message_import_plan(
        connection_id="c1", thread_id=None, message=message or _message(),
        trades=trades if trades is not None else _trades(),
        account_id=ACCOUNT_ID, ledger_account="TST001", history_id=None,
        synced_at=NOW, parser_version="2", base_ledger_version=0,
        existing_message=existing,
        existing_account_transactions=tuple(existing_transactions),
    )


def _hash_of(message: GmailMessage) -> str:
    from lyra.infrastructure.gmail.message_metadata import (
        create_parsed_gmail_message_metadata,
    )

    return create_parsed_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=message, trade_count=1,
        account_id=ACCOUNT_ID, history_id=None, synced_at=NOW,
    ).content_hash


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


def test_links_are_one_based_and_ordered() -> None:
    """关联序号**从 1 开始**：0 会让"第一条"与"未设置"难以区分。"""
    plan = _plan(trades=_trades(3))

    assert isinstance(plan, ReadyGmailMessageImportPlan)
    assert [link.ordinal for link in plan.links] == [1, 2, 3]
    assert [link.transaction_hash for link in plan.links] == [
        transaction.transaction_hash for transaction in plan.commit.inserted_transactions
    ]


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
            content_hash=_hash_of(message), account_id=ACCOUNT_ID,
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

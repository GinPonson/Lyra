"""Gmail 消息元数据测试。

最要紧的一条：**内容摘要必须对"同一封邮件的不同表示"稳定**。

摘要做三处规范化——发件人小写、主题去空白、正文 CRLF → LF。少了任何一处，
同一封邮件重新抓取都会被判定为"内容变了"，触发一次不必要的冲突处理，
**而用户看到的是"这封邮件有冲突"**。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lyra.domain.gmail.econfirm import GmailMessage
from lyra.domain.gmail.sync_state import (
    normalize_gmail_history_id,
    normalize_gmail_sync_timestamp,
)
from lyra.domain.ledger.errors import IngestError
from lyra.infrastructure.gmail.message_metadata import (
    create_parsed_gmail_message_metadata,
    create_unsuccessful_gmail_message_metadata,
    parse_email_timestamp,
)

NOW = datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC)


def _message(**overrides) -> GmailMessage:
    base = {
        "message_id": "m1", "sender": "Schwab <donotreply@mail.schwab.com>",
        "subject": "Schwab eConfirms", "body": "Date: 1/5/2026\nAction: Buy",
        "email_timestamp": "2026-01-05T10:00:00.000Z",
    }
    base.update(overrides)
    return GmailMessage(**base)  # type: ignore[arg-type]


def _hash(message: GmailMessage) -> str:
    return create_parsed_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=message, trade_count=1,
        account_id=None, history_id=None, synced_at=NOW,
    ).content_hash


# --------------------------------------------------------------------------
# 内容摘要的稳定性（核心）
# --------------------------------------------------------------------------


def test_hash_is_stable_across_line_endings() -> None:
    """**CRLF 与 LF 必须得到同一个摘要。**

    换行符差异尤其常见——Gmail API 与原始 RFC 邮件的换行表示不同。
    """
    assert _hash(_message(body="a\r\nb")) == _hash(_message(body="a\nb"))
    assert _hash(_message(body="a\rb")) == _hash(_message(body="a\nb"))


def test_hash_is_stable_across_sender_case_and_whitespace() -> None:
    """发件人大小写与边界空白不影响摘要。"""
    assert _hash(_message(sender="  A@B.COM ")) == _hash(_message(sender="a@b.com"))


def test_hash_is_stable_across_subject_whitespace() -> None:
    """主题边界空白不影响摘要。"""
    assert _hash(_message(subject="  S  ")) == _hash(_message(subject="S"))


def test_hash_changes_when_the_body_changes() -> None:
    """**内容真的变了就必须变**——否则冲突检测失效。"""
    assert _hash(_message(body="a")) != _hash(_message(body="b"))


def test_hash_changes_when_the_subject_changes() -> None:
    """主题变化必须反映在摘要上。"""
    assert _hash(_message(subject="A")) != _hash(_message(subject="B"))


def test_hash_changes_when_the_sender_changes() -> None:
    """发件人变化必须反映在摘要上。"""
    assert _hash(_message(sender="a@b.com")) != _hash(_message(sender="c@d.com"))


# --------------------------------------------------------------------------
# 成功元数据
# --------------------------------------------------------------------------


def test_parsed_metadata_records_the_trade_count() -> None:
    """成功元数据记录成交笔数与 parsed 状态。"""
    result = create_parsed_gmail_message_metadata(
        connection_id="c1", thread_id="t1", message=_message(), trade_count=3,
        account_id="acct", history_id="12345", synced_at=NOW,
    )

    assert result.parse_status == "parsed"
    assert result.error_code is None
    assert result.transaction_count == 3
    assert result.thread_id == "t1"
    assert result.account_id == "acct"
    assert result.history_id == "12345"
    assert result.synced_at == "2026-01-05T10:00:00.000Z"


def test_blank_optional_fields_become_none() -> None:
    """空白可选字段等价于数据库 null。"""
    result = create_parsed_gmail_message_metadata(
        connection_id="c1", thread_id="   ", message=_message(), trade_count=0,
        account_id="", history_id=None, synced_at=NOW,
    )

    assert result.thread_id is None
    assert result.account_id is None


def test_missing_email_timestamp_is_preserved_as_none() -> None:
    """邮件没有时间时保持空，而不是编一个。"""
    result = create_parsed_gmail_message_metadata(
        connection_id="c1", thread_id=None,
        message=_message(email_timestamp=None), trade_count=0,
        account_id=None, history_id=None, synced_at=NOW,
    )

    assert result.received_at is None


def test_required_fields_must_not_be_blank() -> None:
    """连接 ID 与消息 ID 不能为空。"""
    with pytest.raises(ValueError, match="连接 ID"):
        create_parsed_gmail_message_metadata(
            connection_id="  ", thread_id=None, message=_message(), trade_count=0,
            account_id=None, history_id=None, synced_at=NOW,
        )
    with pytest.raises(ValueError, match="message ID"):
        create_parsed_gmail_message_metadata(
            connection_id="c1", thread_id=None, message=_message(message_id=" "),
            trade_count=0, account_id=None, history_id=None, synced_at=NOW,
        )


# --------------------------------------------------------------------------
# 失败元数据
# --------------------------------------------------------------------------


def test_rejected_maps_to_rejected_status() -> None:
    """解析拒绝映射为 rejected。"""
    result = create_unsuccessful_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=_message(), account_id=None,
        history_id=None, synced_at=NOW, error_code="GMAIL_MESSAGE_REJECTED",
    )

    assert result.parse_status == "rejected"
    assert result.transaction_count == 0


@pytest.mark.parametrize(
    "code", ["ACCOUNT_MAPPING_REQUIRED", "GMAIL_MESSAGE_CONFLICT"]
)
def test_mapping_and_conflict_map_to_conflict_status(code: str) -> None:
    """账户映射缺失与冲突都映射为 conflict。

    它们与"解析拒绝"是**两类不同的后续动作**：前者看账户映射，后者看邮件内容。
    """
    result = create_unsuccessful_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=_message(), account_id=None,
        history_id=None, synced_at=NOW, error_code=code,  # type: ignore[arg-type]
    )

    assert result.parse_status == "conflict"
    assert result.error_code == code


def test_failed_metadata_carries_no_body_field() -> None:
    """**失败记录不得包含正文。**

    它只有稳定错误码与摘要——把解析不了的原始内容留在库里没有价值，只有风险。
    """
    result = create_unsuccessful_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=_message(body="secret body"),
        account_id=None, history_id=None, synced_at=NOW,
        error_code="GMAIL_MESSAGE_REJECTED",
    )

    assert not hasattr(result, "body")
    assert "secret body" not in str(result)


def test_success_and_failure_share_the_same_hash() -> None:
    """**摘要与成功路径一致**：这样同一 message ID 的内容变化可以被检测到。"""
    message = _message()
    parsed = create_parsed_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=message, trade_count=1,
        account_id=None, history_id=None, synced_at=NOW,
    )
    failed = create_unsuccessful_gmail_message_metadata(
        connection_id="c1", thread_id=None, message=message, account_id=None,
        history_id=None, synced_at=NOW, error_code="GMAIL_MESSAGE_REJECTED",
    )

    assert parsed.content_hash == failed.content_hash


# --------------------------------------------------------------------------
# 时间戳
# --------------------------------------------------------------------------


def test_epoch_milliseconds_are_accepted() -> None:
    """纯数字按毫秒解释。"""
    assert parse_email_timestamp("1767607200000") == "2026-01-05T10:00:00.000Z"


def test_iso_text_is_accepted() -> None:
    """ISO 文本被规范为 UTC。"""
    assert parse_email_timestamp("2026-01-05T12:00:00+02:00") == "2026-01-05T10:00:00.000Z"


def test_rfc2822_text_is_accepted() -> None:
    """RFC 2822 文本（Gmail Date 头格式）被接受。"""
    assert parse_email_timestamp("Mon, 5 Jan 2026 10:00:00 +0000") == (
        "2026-01-05T10:00:00.000Z"
    )


@pytest.mark.parametrize("value", ["not a date", "  ", "999999999999999999999"])
def test_invalid_timestamps_are_rejected(value: str) -> None:
    """无法解析的时间戳必须报错，而不是静默当成没有时间。"""
    with pytest.raises(IngestError, match="时间戳无效"):
        parse_email_timestamp(value)


# --------------------------------------------------------------------------
# 游标与同步时间
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "12345", "999999999999"])
def test_valid_history_ids(value: str) -> None:
    """正十进制字符串通过。"""
    assert normalize_gmail_history_id(value) == value


@pytest.mark.parametrize("value", ["0", "0123", "-1", "abc", ""])
def test_invalid_history_ids(value: str) -> None:
    """**无前导零**：否则同一个游标有两种写法，增量比较会误判为"变了"。"""
    with pytest.raises(ValueError):
        normalize_gmail_history_id(value)


def test_sync_timestamp_requires_utc_marker() -> None:
    """**必须显式以 Z 结尾**：没有时会话时区会让同一瞬时产生不同文本。"""
    assert normalize_gmail_sync_timestamp("2026-01-05T10:00:00.000Z").endswith("Z")

    with pytest.raises(ValueError, match="UTC"):
        normalize_gmail_sync_timestamp("2026-01-05T10:00:00+00:00")

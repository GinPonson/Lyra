"""Gmail ``format=full`` 解析测试。

**正文是 base64url 编码的，字符集来自部件自己的 ``Content-Type`` 头。** 假定 UTF-8
会在供应商返回别的字符集时把正文变成乱码——而乱码不会报错，只会在 eConfirm 解析时
表现为"找不到字段"，让人往错误的方向排查。
"""

from __future__ import annotations

import base64

import pytest

from lyra.domain.ledger.errors import IngestError
from lyra.infrastructure.gmail.full_message import parse_gmail_full_input

SCHWAB_FROM = "Schwab <donotreply@mail.schwab.com>"
SCHWAB_SUBJECT = "Schwab eConfirms"


def _encoded(text: str, charset: str = "utf-8") -> str:
    """把文本按 base64url 无填充编码。"""
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def _record(
    *,
    message_id: str = "m1",
    body: str = "Date: 1/5/2026",
    charset: str = "utf-8",
    sender: str = SCHWAB_FROM,
    subject: str = SCHWAB_SUBJECT,
    date: str | None = "Mon, 5 Jan 2026 10:00:00 +0000",
    internal_date: object = "1767607200000",
    mime_type: str = "text/plain",
    payload: object = None,
) -> dict:
    """构造一条 ``format=full`` 记录。"""
    if payload is not None:
        return {"id": message_id, "internalDate": internal_date, "payload": payload}
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Content-Type", "value": f"{mime_type}; charset={charset}"},
    ]
    if date is not None:
        headers.append({"name": "Date", "value": date})
    return {
        "id": message_id,
        "internalDate": internal_date,
        "payload": {
            "mimeType": mime_type, "headers": headers,
            "body": {"data": _encoded(body, charset)},
        },
    }


def _parse(payload):
    """跑一次解析。"""
    return parse_gmail_full_input(payload, source_name="s")


# --------------------------------------------------------------------------
# 正常
# --------------------------------------------------------------------------


def test_parses_a_single_record() -> None:
    """单条记录解析出全部字段。"""
    message = _parse(_record())[0]

    assert message.message_id == "m1"
    assert message.sender == SCHWAB_FROM
    assert message.subject == SCHWAB_SUBJECT
    assert message.body == "Date: 1/5/2026"


def test_parses_an_array_of_records() -> None:
    """数组记录逐条解析。"""
    messages = _parse([_record(message_id="m1"), _record(message_id="m2")])

    assert [m.message_id for m in messages] == ["m1", "m2"]


def test_empty_array_is_rejected() -> None:
    """空数组报错。"""
    with pytest.raises(IngestError, match="没有邮件"):
        _parse([])


# --------------------------------------------------------------------------
# 入口校验
# --------------------------------------------------------------------------


def test_list_response_is_rejected_with_guidance() -> None:
    """**拒绝 ``messages.list`` 的响应**，并说明该调哪个接口。"""
    with pytest.raises(IngestError) as raised:
        _parse({"messages": [{"id": "m1"}]})

    assert "只有 message id" in str(raised.value)
    assert "format=full" in str(raised.value)


def test_raw_format_is_rejected_with_guidance() -> None:
    """**``format=raw`` 需要 MIME 适配器**，不能当 full 处理。"""
    with pytest.raises(IngestError, match="RFC 5322 MIME 适配器"):
        _parse({"id": "m1", "raw": "From: ..."})


def test_missing_payload_is_rejected() -> None:
    """既没有 payload 也没有 raw 时说明响应形状不符。"""
    with pytest.raises(IngestError, match="不是 Gmail"):
        _parse({"id": "m1"})


def test_non_object_record_is_rejected() -> None:
    """记录不是对象时报错。"""
    with pytest.raises(IngestError, match="邮件必须是 JSON 对象"):
        _parse(["not-an-object"])


def test_missing_message_id_is_rejected() -> None:
    """缺 id 报错。"""
    record = _record()
    del record["id"]

    with pytest.raises(IngestError, match="缺少 Gmail message id"):
        _parse(record)


# --------------------------------------------------------------------------
# 来源校验
# --------------------------------------------------------------------------


def test_non_schwab_sender_is_rejected() -> None:
    """非 Schwab 发件人报错。"""
    with pytest.raises(IngestError, match="发件人不是可识别的 Schwab 域名"):
        _parse(_record(sender="attacker@example.com"))


def test_lookalike_sender_is_rejected() -> None:
    """伪装域名同样拒绝。"""
    with pytest.raises(IngestError, match="发件人不是"):
        _parse(_record(sender="donotreply@mail.schwab.com.attacker.net"))


def test_wrong_subject_is_rejected() -> None:
    """主题不符报错。"""
    with pytest.raises(IngestError, match="不是 Schwab eConfirms 邮件"):
        _parse(_record(subject="Account statement"))


# --------------------------------------------------------------------------
# 正文与字符集
# --------------------------------------------------------------------------


def test_body_without_data_is_rejected_with_guidance() -> None:
    """**没有内联正文时报错并说明要先取附件。**"""
    record = _record()
    record["payload"]["body"] = {"attachmentId": "att-1", "size": 5000}

    with pytest.raises(IngestError) as raised:
        _parse(record)

    assert "不含内联" in str(raised.value)
    assert "attachment data" in str(raised.value)


def test_non_utf8_charset_is_honoured() -> None:
    """**字符集取自部件的 Content-Type。**

    假定 UTF-8 会在别的字符集下把正文变成乱码，而乱码不会报错。
    """
    # 用 latin-1 里确实存在的字符（é = 0xE9）；在 UTF-8 下会变成两个字节，
    # 按 UTF-8 解码会得到乱码而不是报错。
    message = _parse(_record(body="Date: 1/5/2026 INTELé", charset="latin-1"))[0]

    assert message.body == "Date: 1/5/2026 INTELé"


def test_undecodable_body_is_rejected_not_replaced() -> None:
    """**解码失败报错而不是替换成 ``�``。**

    替换字符可能吃掉符号或金额里的字符，而结果看起来仍然"解析成功"。
    """
    record = _record()
    # 让字节序列在 UTF-8 下非法（孤立的高位字节）。
    record["payload"]["body"]["data"] = base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("=")

    with pytest.raises(IngestError, match="无法按 charset"):
        _parse(record)


def test_invalid_base64_data_is_rejected() -> None:
    """字符集不符或长度非法的 base64 报错。"""
    record = _record()
    record["payload"]["body"]["data"] = "not base64!"

    with pytest.raises(IngestError, match="Base64URL 数据无效"):
        _parse(record)


def test_html_body_is_converted() -> None:
    """HTML 正文被转换为文本。"""
    html = "<div>Date: 1/5/2026</div><div>Action: Buy</div>"
    message = _parse(_record(body=html, mime_type="text/html"))[0]

    assert message.body == "Date: 1/5/2026\n\nAction: Buy"


def test_nested_parts_are_collected() -> None:
    """嵌套部件里的文本被收集。"""
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [],
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "headers": [], "body": {"data": _encoded("plain")}},
            {"mimeType": "text/html", "headers": [], "body": {"data": _encoded("<p>html</p>")}},
        ],
    }
    record = _record(payload=payload)
    record["payload"]["headers"] = [
        {"name": "From", "value": SCHWAB_FROM},
        {"name": "Subject", "value": SCHWAB_SUBJECT},
    ]

    message = _parse(record)[0]

    # 纯文本优先，HTML 作为候选。
    assert message.body == "plain"
    assert message.body_candidates == ("html",)


def test_part_without_mime_type_is_rejected() -> None:
    """部件缺 ``mimeType`` 报错。"""
    payload = {"headers": [], "body": {}}

    with pytest.raises(IngestError, match="缺少 mimeType"):
        _parse(_record(payload=payload))


def test_non_array_parts_is_rejected() -> None:
    """``parts`` 不是数组报错。"""
    payload = {"mimeType": "multipart/mixed", "headers": [], "body": {}, "parts": "nope"}

    with pytest.raises(IngestError, match="parts 必须是数组"):
        _parse(_record(payload=payload))


def test_missing_headers_array_is_rejected() -> None:
    """缺 ``headers`` 数组报错。"""
    payload = {"mimeType": "text/plain", "body": {}}

    with pytest.raises(IngestError, match="缺少标准 headers 数组"):
        _parse(_record(payload=payload))


def test_malformed_header_entry_is_rejected() -> None:
    """头部项必须含字符串 name/value。"""
    payload = {"mimeType": "text/plain", "headers": [{"name": "From"}], "body": {}}

    with pytest.raises(IngestError, match="必须包含字符串 name/value"):
        _parse(_record(payload=payload))


# --------------------------------------------------------------------------
# 时间戳
# --------------------------------------------------------------------------


def test_date_header_takes_priority() -> None:
    """**Date 头优先**，它才是邮件自身声明的时间。"""
    message = _parse(_record(date="Mon, 5 Jan 2026 10:00:00 +0000"))[0]

    assert message.email_timestamp == "Mon, 5 Jan 2026 10:00:00 +0000"


def test_internal_date_is_used_when_the_date_header_is_absent() -> None:
    """没有 Date 头时退回 ``internalDate``。"""
    message = _parse(_record(date=None, internal_date="1767607200000"))[0]

    assert message.email_timestamp == "1767607200000"


def test_no_timestamp_at_all_yields_none() -> None:
    """两者都没有时为空，而不是编一个。"""
    message = _parse(_record(date=None, internal_date=None))[0]

    assert message.email_timestamp is None


def test_empty_date_header_falls_back() -> None:
    """空的 Date 头视为缺失。"""
    message = _parse(_record(date="", internal_date="1767607200000"))[0]

    assert message.email_timestamp == "1767607200000"

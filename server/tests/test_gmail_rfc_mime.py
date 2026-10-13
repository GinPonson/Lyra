"""MIME 解析测试。

**消息 ID 的回退顺序是幂等性的基础**：

```
X-GM-MSGID → X-Gmail-Message-ID → Message-ID → sha256(原始字节)
```

同一封邮件重复处理必须得到同一个 ID，否则重复同步会产生重复交易。
"""

from __future__ import annotations

import hashlib

import pytest

from lyra.domain.ledger.errors import IngestError
from lyra.infrastructure.gmail.html_text import (
    html_to_text,
    normalize_email_body_candidates,
)
from lyra.infrastructure.gmail.rfc_mime import parse_rfc_mime_message


def _message(
    *,
    sender: str = "Schwab <donotreply@mail.schwab.com>",
    subject: str = "Schwab eConfirms",
    body: str = "Date: 1/5/2026\nAction: Buy\n",
    content_type: str = "text/plain; charset=utf-8",
    message_id: str | None = "<id-1@mail.schwab.com>",
    date: str | None = "Mon, 5 Jan 2026 10:00:00 +0000",
    extra: str = "",
) -> bytes:
    """构造一封原始邮件。"""
    lines = [f"From: {sender}", f"Subject: {subject}"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    if date is not None:
        lines.append(f"Date: {date}")
    if extra:
        lines.append(extra)
    lines.append(f"Content-Type: {content_type}")
    lines.append("")
    lines.append(body)
    return ("\r\n".join(lines)).encode("utf-8")


# --------------------------------------------------------------------------
# 正常解析
# --------------------------------------------------------------------------


def test_parses_a_valid_econfirm() -> None:
    """合法邮件解析出发件人、主题、正文与时间。"""
    parsed = parse_rfc_mime_message(_message(), source_name="a.eml")

    assert parsed.sender == "Schwab <donotreply@mail.schwab.com>"
    assert parsed.subject == "Schwab eConfirms"
    assert "Action: Buy" in parsed.body
    assert parsed.email_timestamp == "2026-01-05T10:00:00.000Z"


def test_html_body_is_converted_to_text() -> None:
    """HTML 正文被转换为保留字段边界的文本。"""
    html = "<div>Date: 1/5/2026</div><div>Action: Buy</div>"
    parsed = parse_rfc_mime_message(
        _message(body=html, content_type="text/html; charset=utf-8"),
        source_name="a.eml",
    )

    assert parsed.body == "Date: 1/5/2026\n\nAction: Buy"


def test_missing_date_yields_no_timestamp() -> None:
    """没有 Date 头时时间为空，而不是编一个。"""
    parsed = parse_rfc_mime_message(_message(date=None), source_name="a.eml")
    assert parsed.email_timestamp is None


def test_subject_may_have_a_batch_suffix() -> None:
    """主题后追加批次信息时仍能识别（用包含判断）。"""
    parsed = parse_rfc_mime_message(
        _message(subject="Schwab eConfirms - January 2026"), source_name="a.eml"
    )
    assert parsed.subject.endswith("January 2026")


# --------------------------------------------------------------------------
# 消息 ID 回退链
# --------------------------------------------------------------------------


def test_message_id_comes_from_the_message_id_header() -> None:
    """有 Message-ID 时用它，并去掉尖括号。"""
    parsed = parse_rfc_mime_message(_message(), source_name="a.eml")
    assert parsed.message_id == "id-1@mail.schwab.com"


def test_gmail_extension_header_wins_over_message_id() -> None:
    """Gmail 扩展头优先——它是邮箱内的稳定标识。"""
    parsed = parse_rfc_mime_message(
        _message(extra="X-GM-MSGID: 1234567890"), source_name="a.eml"
    )
    assert parsed.message_id == "1234567890"


def test_content_hash_is_the_last_resort() -> None:
    """没有任何标识头时用原始字节摘要——保证重复处理得到同一 ID。"""
    raw = _message(message_id=None)
    parsed = parse_rfc_mime_message(raw, source_name="a.eml")

    assert parsed.message_id == hashlib.sha256(raw).hexdigest()
    # 同一份字节重复解析得到同一个 ID。
    assert parse_rfc_mime_message(raw, source_name="a.eml").message_id == parsed.message_id


def test_duplicate_identifier_header_is_rejected() -> None:
    """**重复的标识头产生不明确的身份**：取第一个还是最后一个都是猜测。"""
    raw = _message(message_id=None, extra="X-GM-MSGID: 111\r\nX-GM-MSGID: 222")

    with pytest.raises(IngestError, match="必须是唯一字符串"):
        parse_rfc_mime_message(raw, source_name="a.eml")


def test_raw_header_injection_is_neutralised_by_the_parser() -> None:
    """原始字节里的 CRLF **构不成头注入**——解析器会把它拆成独立头。

    这条测试记录真实行为：`Injected` 成为另一个头，`X-GM-MSGID` 的值仍只是 `a`。
    真正的保护来自 MIME 解析器，下面的守卫是纵深防御。
    """
    raw = _message(message_id=None, extra="X-GM-MSGID: a\r\nInjected: b")

    parsed = parse_rfc_mime_message(raw, source_name="a.eml")

    assert parsed.message_id == "a"


def test_identifier_guard_rejects_line_breaks_directly() -> None:
    """守卫本身必须拒绝含换行的标识。

    解析器通常不会产出这种值，但守卫是可复用的——它保护的是"标识被写进日志或下游
    协议"这条路径，而不是解析这一处。
    """
    from lyra.infrastructure.gmail.rfc_mime import _normalize_identifier

    for value in ("a\r\nb", "a\nb", "<a\nb>"):
        with pytest.raises(IngestError, match="标识无效"):
            _normalize_identifier(value, "a.eml")


# --------------------------------------------------------------------------
# 来源校验
# --------------------------------------------------------------------------


def test_rejects_a_non_schwab_sender() -> None:
    """其它发件人必须拒绝。"""
    with pytest.raises(IngestError, match="发件人不是"):
        parse_rfc_mime_message(
            _message(sender="attacker@example.com"), source_name="a.eml"
        )


@pytest.mark.parametrize(
    "sender",
    [
        "attacker@mail.schwab.com.attacker.net",       # 域名后缀伪装
        "donotreply@mail.schwab.com.attacker.net",
        "evil-donotreply@mail.schwab.com",             # 本地部分伪装
    ],
)
def test_rejects_lookalike_senders(sender: str) -> None:
    """**伪装地址必须拒绝。**

    宽松的"包含即匹配"会让 `donotreply@mail.schwab.com.attacker.net` 通过——
    它包含目标字符串但不是同一个地址。
    """
    with pytest.raises(IngestError, match="发件人不是"):
        parse_rfc_mime_message(_message(sender=sender), source_name="a.eml")


def test_rejects_a_non_econfirm_subject() -> None:
    """主题不符必须拒绝。"""
    with pytest.raises(IngestError, match="不是 Schwab eConfirms"):
        parse_rfc_mime_message(
            _message(subject="Account statement"), source_name="a.eml"
        )


def test_rejects_empty_content() -> None:
    """空内容必须拒绝。"""
    with pytest.raises(IngestError, match="不能为空"):
        parse_rfc_mime_message(b"", source_name="a.eml")


def test_rejects_missing_body() -> None:
    """没有内联正文时报错，而不是产出一封空邮件。"""
    raw = _message(body="", content_type="application/octet-stream")
    with pytest.raises(IngestError, match="没有内联"):
        parse_rfc_mime_message(raw, source_name="a.eml")


def test_rejects_invalid_date() -> None:
    """无法解析的 Date 头必须报错，而不是静默当成没有时间。"""
    with pytest.raises(IngestError, match="Date 头无效"):
        parse_rfc_mime_message(_message(date="not a date"), source_name="a.eml")


def test_rejects_unsafe_source_name() -> None:
    """来源名不得含路径或控制字符。"""
    for name in ("", "  ", "a/b.eml", "a\\b.eml", "a\nb"):
        with pytest.raises(IngestError, match="来源名无效"):
            parse_rfc_mime_message(_message(), source_name=name)


# --------------------------------------------------------------------------
# HTML 与正文候选
# --------------------------------------------------------------------------


def test_html_to_text_separates_blocks_with_double_newlines() -> None:
    """**块之间用双换行**：单换行在 eConfirm 正文里是同一字段的续行。"""
    assert html_to_text("<div>A</div><div>B</div>") == "A\n\nB"


def test_html_to_text_drops_scripts_and_styles() -> None:
    """不可见的脚本与样式内容必须删除。"""
    markup = "<style>p{color:red}</style><script>evil()</script><p>Visible</p>"
    assert html_to_text(markup) == "Visible"


def test_html_to_text_decodes_entities() -> None:
    """常见实体被解码；未知实体**原样保留**。"""
    assert html_to_text("<p>a &amp; b &lt;c&gt; &#65; &#x42;</p>") == "a & b <c> A B"
    assert html_to_text("<p>&unknown;</p>") == "&unknown;"


def test_html_to_text_collapses_inline_whitespace() -> None:
    """行内空白折叠为单个空格并去除行首尾空白。"""
    assert html_to_text("<p>a    b\t\tc</p>") == "a b c"


def test_body_candidates_prefer_plain_text() -> None:
    """**纯文本候选排在 HTML 之前**：它是供应商给出的原文，不经转换。"""
    candidates = normalize_email_body_candidates((
        ("text/html", "<p>H</p>"), ("text/plain", "P"),
    ))

    assert candidates == ("P", "H")


def test_body_candidates_skip_empty_and_duplicate() -> None:
    """空正文与重复正文都不作为候选。"""
    candidates = normalize_email_body_candidates((
        ("text/plain", "   "), ("text/plain", "P"), ("text/plain", "P"),
    ))

    assert candidates == ("P",)


def test_extra_candidates_are_exposed() -> None:
    """HTML 版本作为候选保留，供后续解析器在纯文本解析失败时尝试。"""
    raw = _message(
        body=("<p>plain</p>"), content_type="multipart/alternative; boundary=B"
    )
    parsed = parse_rfc_mime_message(
        _message(
            body=(
                "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain\r\n"
                "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>html</p>\r\n"
                "--B--\r\n"
            ),
            content_type="multipart/alternative; boundary=B",
        ),
        source_name="a.eml",
    )

    assert parsed.body == "plain"
    assert parsed.body_candidates == ("html",)
    del raw

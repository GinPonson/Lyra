"""原始 RFC MIME 邮件的解析。

镜像 TypeScript 版 ``infrastructure/gmail/rfc-mime.ts``。

**消息 ID 的回退顺序是幂等性的基础**：

```
X-GM-MSGID  →  X-Gmail-Message-ID  →  Message-ID  →  sha256(原始字节)
```

前面几项都是邮件自身的标识，因此**同一封邮件重复处理会得到同一个 ID**；最后一项
兜底保证任何邮件都有一个稳定标识。少了这条链，重复同步会产生重复交易。

两处防御：

- **自定义标识头必须是唯一字符串**。重复头会产生不明确的身份——取第一个还是最后一个？
  任何选择都是猜测。
- **标识不得含 CR/LF**，否则把它写进日志或下游协议时会造成头注入。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime

from ...domain.gmail.econfirm import (
    SCHWAB_ECONFIRM_SUBJECT,
    GmailMessage,
    is_schwab_sender,
)
from ...domain.ledger.errors import IngestError
from .html_text import normalize_email_body_candidates

_CONTROL = re.compile(r"[\r\n]")


def parse_rfc_mime_message(content: bytes, *, source_name: str) -> GmailMessage:
    """解析原始 RFC MIME 邮件。

    @param content: 邮件原始字节。
    @param source_name: 仅用于错误定位的来源名。
    @returns 已解析且已验证来源的邮件。
    @raises IngestError: 内容为空、无法解析、来源不符，或缺少正文。
    """
    name = _require_source_name(source_name)
    if len(content) == 0:
        raise IngestError(f"{name}: MIME 邮件不能为空")

    try:
        parsed = message_from_bytes(content, policy=default_policy)
    except Exception as error:
        raise IngestError(f"{name}: 无法解析原始 RFC MIME 邮件") from error

    sender = str(parsed.get("From") or "").strip()
    subject = str(parsed.get("Subject") or "").strip()
    if not is_schwab_sender(sender):
        raise IngestError(f"{name}: 发件人不是 Schwab 通知地址")
    if SCHWAB_ECONFIRM_SUBJECT not in subject:
        raise IngestError(f"{name}: 不是 Schwab eConfirms 邮件")

    parts: list[tuple[str, str]] = []
    for mime_type, body in _body_parts(parsed):
        parts.append((mime_type, body))
    candidates = normalize_email_body_candidates(tuple(parts))
    if not candidates:
        # 没有正文就没有可解析的内容；报错而不是产出一封空邮件。
        raise IngestError(f"{name}: MIME 中没有内联 text/plain 或 text/html 正文")

    body, *rest = candidates
    return GmailMessage(
        message_id=_resolve_message_id(parsed, content, name),
        sender=sender,
        subject=subject,
        body=body,
        email_timestamp=_resolve_timestamp(parsed, name),
        body_candidates=tuple(rest),
    )


def _body_parts(message: Message) -> list[tuple[str, str]]:
    """收集内联的 ``text/plain`` 与 ``text/html`` 部分。

    只看非附件部分：附件里的文本不是邮件正文，把它们当正文会让解析器读到无关内容。
    """
    parts: list[tuple[str, str]] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            if part.get_content_maintype() != "text":
                continue
            parts.append((part.get_content_type(), _decode_part(part)))
        return parts
    if message.get_content_maintype() == "text":
        parts.append((message.get_content_type(), _decode_part(message)))
    return parts


def _decode_part(part: Message) -> str:
    """按声明的字符集解码一个部分。

    **解码失败返回空串**而不是抛错：一个字符集标注错误的正文不该让整封邮件无法处理，
    而空正文会由"缺少正文"那条检查拦下。
    """
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        # 未知字符集名：退回 UTF-8 并替换非法字节。
        return payload.decode("utf-8", errors="replace")


def _resolve_message_id(parsed: Message, content: bytes, source_name: str) -> str:
    """按 Gmail 扩展头 → Message-ID → 原文摘要的顺序生成幂等消息标识。"""
    for header in ("X-GM-MSGID", "X-Gmail-Message-ID"):
        raw = parsed.get_all(header)
        if raw is None:
            continue
        if len(raw) != 1:
            # 重复头产生不明确的身份：取第一个还是最后一个都是猜测。
            raise IngestError(f"{source_name}: MIME 消息标识头必须是唯一字符串")
        normalized = _normalize_identifier(str(raw[0]), source_name)
        if normalized is not None:
            return normalized
    normalized = _normalize_identifier(parsed.get("Message-ID"), source_name)
    if normalized is not None:
        return normalized
    # 兜底：内容摘要保证任何邮件都有稳定标识。
    return hashlib.sha256(content).hexdigest()


def _normalize_identifier(value: object, source_name: str) -> str | None:
    """去除标准尖括号并拒绝空值或折行注入。"""
    if value is None:
        return None
    trimmed = str(value).strip()
    normalized = (
        trimmed[1:-1].strip()
        if trimmed.startswith("<") and trimmed.endswith(">")
        else trimmed
    )
    if normalized == "" or _CONTROL.search(normalized):
        # 含换行的标识写进日志或下游协议时会造成头注入。
        raise IngestError(f"{source_name}: MIME 消息标识无效")
    return normalized


def _resolve_timestamp(parsed: Message, source_name: str) -> str | None:
    """把 ``Date`` 头规范为可持久化的 UTC 时间。

    @returns ISO 文本；没有 ``Date`` 头时为 ``None``。
    @raises IngestError: ``Date`` 头存在但无法解析。
    """
    raw = parsed.get("Date")
    if raw is None:
        return None
    try:
        parsed_date = parsedate_to_datetime(str(raw))
    except Exception as error:
        raise IngestError(f"{source_name}: MIME Date 头无效") from error
    if parsed_date is None:
        raise IngestError(f"{source_name}: MIME Date 头无效")
    if parsed_date.tzinfo is None:
        # 无时区的 Date 头无法定位到时间线上的某一点；按 UTC 解释是猜测，
        # 但报错会让整封邮件无法处理。这里按 UTC 处理并保持可预测。
        parsed_date = parsed_date.replace(tzinfo=UTC)
    return (
        parsed_date.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _require_source_name(value: str) -> str:
    """来源名只用于安全错误定位，禁止路径或控制字符进入客户端消息。"""
    name = value.strip()
    if name == "" or _CONTROL.search(name) or "/" in name or "\\" in name:
        raise IngestError("MIME 来源名无效")
    return name

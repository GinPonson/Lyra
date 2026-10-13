"""Gmail ``format=full`` 响应 → 标准邮件。

镜像 TypeScript 版 ``infrastructure/gmail/full-message.ts``。

**正文是 base64url 编码的，字符集来自部件自己的 ``Content-Type`` 头。** 假定 UTF-8
会在供应商返回别的字符集时把正文变成乱码——而乱码不会报错，只会在 eConfirm 解析时
表现为"找不到字段"，让人往错误的方向排查。

**解码是严格模式**：非法字节报错而不是替换成 ``�``。替换字符可能吃掉一个符号或金额
里的字符，而结果看起来仍然"解析成功"。

三处入口校验，各自防一种误用：

1. **拒绝 ``messages.list`` 的响应**——它只有 id，必须继续调 ``messages.get``；
2. **拒绝 ``format=raw``**——那是 RFC 5322 原文，需要 MIME 适配器；
3. **没有内联正文时报错**并说明附件式正文要先取附件。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field

from ...domain.gmail.econfirm import (
    SCHWAB_ECONFIRM_SUBJECT,
    GmailMessage,
    is_schwab_sender,
)
from ...domain.ledger.errors import IngestError
from .html_text import normalize_email_body_candidates

TEXT_MIME_TYPES = ("text/plain", "text/html")

# 从部件的 Content-Type 头里取字符集。
_CHARSET = re.compile(r"""charset=["']?([^;\s"']+)""", re.IGNORECASE)


@dataclass(frozen=True)
class GmailHeader:
    """一个 MIME 头。"""

    name: str
    value: str


@dataclass(frozen=True)
class GmailPart:
    """一个 MIME 部件。"""

    mime_type: str
    headers: tuple[GmailHeader, ...]
    body: dict
    parts: tuple[GmailPart, ...] = field(default_factory=tuple)


def parse_gmail_full_input(payload: object, *, source_name: str) -> tuple[GmailMessage, ...]:
    """解析 ``users.messages.get(format=full)`` 的响应。

    接受单个记录或记录数组。

    @param payload: 响应负载。
    @param source_name: 错误定位说明。
    @returns 邮件序列（每封都已完成来源与主题校验）。
    @raises IngestError: 是 list 响应、缺 payload、来源不符或没有正文。
    """
    if isinstance(payload, dict) and "messages" in payload:
        # 只有 id 的列表响应无法解析成邮件——直接说明该调哪个接口。
        raise IngestError(
            f"{source_name}: Gmail users.messages.list 只有 message id；"
            "必须继续调用 users.messages.get(format=full)"
        )
    records = payload if isinstance(payload, list) else [payload]
    if len(records) == 0:
        raise IngestError(f"{source_name}: Gmail JSON 中没有邮件")
    return tuple(
        _parse_full_record(record, f"{source_name} 第{index + 1}封")
        for index, record in enumerate(records)
    )


def _parse_full_record(record: object, where: str) -> GmailMessage:
    """解析单封邮件。"""
    if not isinstance(record, dict):
        raise IngestError(f"{where}: 邮件必须是 JSON 对象")
    message_id = _required_string(record.get("id"), f"{where}: 缺少 Gmail message id")
    raw_payload = record.get("payload")
    if not isinstance(raw_payload, dict):
        if isinstance(record.get("raw"), str):
            raise IngestError(f"{where}: format=raw 需要 RFC 5322 MIME 适配器解析")
        raise IngestError(
            f"{where}: 不是 Gmail users.messages.get(format=full) 标准响应"
        )

    part = _parse_part(raw_payload, f"{where} payload")
    headers = _header_map(part.headers)
    sender = headers.get("from", "")
    subject = headers.get("subject", "")
    if not is_schwab_sender(sender):
        raise IngestError(f"{where}: 发件人不是可识别的 Schwab 域名: {sender!r}")
    if SCHWAB_ECONFIRM_SUBJECT not in subject:
        raise IngestError(f"{where}: 不是 Schwab eConfirms 邮件: {subject!r}")

    candidates = normalize_email_body_candidates(_collect_text_parts(part, where))
    if not candidates:
        raise IngestError(
            f"{where}: Gmail format=full 不含内联 text/plain 或 text/html 正文；"
            "附件式正文必须先通过 Gmail API 取得 attachment data"
        )
    body, *rest = candidates

    date_header = headers.get("date")
    internal_date = record.get("internalDate")
    timestamp = (
        str(internal_date)
        if isinstance(internal_date, (str, int)) and not isinstance(internal_date, bool)
        else None
    )
    return GmailMessage(
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
        # Date 头优先；缺失或为空时退回 internalDate。
        email_timestamp=(date_header if date_header else timestamp),
        body_candidates=tuple(rest),
    )


def _parse_part(value: dict, where: str) -> GmailPart:
    """递归解析一个 MIME 部件。"""
    mime_type = str(value.get("mimeType") or "").lower()
    if mime_type == "":
        raise IngestError(f"{where}: Gmail MIME part 缺少 mimeType")
    headers = _parse_headers(value.get("headers"), where)
    body = value.get("body") if isinstance(value.get("body"), dict) else {}
    raw_parts = value.get("parts")
    if raw_parts is not None and not isinstance(raw_parts, list):
        raise IngestError(f"{where}: Gmail MIME parts 必须是数组")
    children: list[GmailPart] = []
    for index, child in enumerate(raw_parts or []):
        if not isinstance(child, dict):
            raise IngestError(f"{where}: Gmail MIME part {index + 1} 必须是对象")
        children.append(_parse_part(child, f"{where} part {index + 1}"))
    return GmailPart(
        mime_type=mime_type, headers=headers, body=body, parts=tuple(children)
    )


def _parse_headers(value: object, where: str) -> tuple[GmailHeader, ...]:
    """解析头部数组。

    **必须是数组**：头部是 Gmail 的稳定契约，非数组说明响应形状已变，
    猜它的替代形状没有依据。
    """
    if not isinstance(value, list):
        raise IngestError(f"{where}: Gmail payload 缺少标准 headers 数组")
    headers: list[GmailHeader] = []
    for index, header in enumerate(value):
        if (
            not isinstance(header, dict)
            or not isinstance(header.get("name"), str)
            or not isinstance(header.get("value"), str)
        ):
            raise IngestError(f"{where}: Gmail header {index + 1} 必须包含字符串 name/value")
        headers.append(GmailHeader(name=header["name"], value=header["value"]))
    return tuple(headers)


def _collect_text_parts(part: GmailPart, where: str) -> tuple[tuple[str, str], ...]:
    """收集内联的文本部件并解码。

    只有带非空 ``body.data`` 的文本部件才算——附件式正文（只有 ``attachmentId``）
    必须先前置水合（见 ``google_gmail_client.hydrate_gmail_text_attachments``）。

    @returns ``(mime_type, 解码后文本)`` 序列。
    @raises IngestError: 数据非法或无法按声明的字符集解码。
    """
    results: list[tuple[str, str]] = []
    if part.mime_type in TEXT_MIME_TYPES:
        data = part.body.get("data")
        if isinstance(data, str) and data != "":
            content_type = next(
                (
                    header.value for header in part.headers
                    if header.name.lower() == "content-type"
                ),
                "",
            )
            charset = (_CHARSET.search(content_type).group(1) if _CHARSET.search(content_type) else None) or "utf-8"
            decoded = _decode_base64url(data, where)
            try:
                # **严格解码**：替换成 � 可能吃掉符号或金额里的字符。
                content = decoded.decode(charset, errors="strict")
            except (UnicodeDecodeError, LookupError) as error:
                raise IngestError(
                    f"{where}: 无法按 charset={charset!r} 解码邮件正文"
                ) from error
            results.append((part.mime_type, content))
    for child in part.parts:
        results.extend(_collect_text_parts(child, where))
    return tuple(results)


def _decode_base64url(value: str, where: str) -> bytes:
    """严格解码无填充 base64url。

    @raises IngestError: 字符集不符或长度非法。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]*", value) or len(value) % 4 == 1:
        raise IngestError(f"{where}: Gmail Base64URL 数据无效")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as error:
        raise IngestError(f"{where}: Gmail Base64URL 数据无效") from error


def _header_map(headers: tuple[GmailHeader, ...]) -> dict[str, str]:
    """把头部数组转为不区分大小写的映射（重复头取最后一个）。"""
    return {header.name.lower(): header.value for header in headers}


def _required_string(value: object, message: str) -> str:
    """读取必填非空字符串。"""
    if not isinstance(value, str) or value.strip() == "":
        raise IngestError(message)
    return value.strip()

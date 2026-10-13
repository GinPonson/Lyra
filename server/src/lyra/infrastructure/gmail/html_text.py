"""受信邮件 HTML 转文本。

镜像 TypeScript 版 ``full-message.ts`` 的 ``htmlToText``。

**这不是通用浏览器渲染器**，只满足 eConfirm 确定性解析器的结构要求：

1. 删除不可见的 ``<script>`` / ``<style>`` 内容；
2. 在已确认的块标签处**插入换行**——eConfirm 的字段边界靠块结构建立；
3. 解码常见字符实体；
4. 折叠行内空白、**丢弃空行**，最后用**双换行**连接。

双换行是刻意的：单个换行在 eConfirm 正文里是"同一字段的续行"，而块之间必须是段落级
分隔。用单换行连接会让两个不同字段被解析成一个。
"""

from __future__ import annotations

import re

BLOCK_TAGS: tuple[str, ...] = (
    "address", "article", "br", "div", "footer", "h1", "h2", "h3", "h4",
    "header", "li", "p", "section", "table", "td", "th", "tr", "ul",
)

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1\s*>", re.IGNORECASE)
_BLOCK = re.compile(
    rf"</?(?:{'|'.join(BLOCK_TAGS)})\b[^>]*>", re.IGNORECASE
)
_TAG = re.compile(r"<[^>]*>")
_INLINE_SPACE = re.compile(r"[ \t\f\v]+")
_ENTITY = re.compile(r"&(#x[0-9a-f]+|#\d+|[a-z]+);", re.IGNORECASE)

# 只解码 eConfirm 字段可能使用的标准实体；未知实体原样保留。
_NAMED_ENTITIES = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " ",
}


def html_to_text(markup: str) -> str:
    """把受信邮件 HTML 转为保留字段边界的文本。

    @param markup: HTML 正文。
    @returns 规范化后的文本。
    """
    without_invisible = _SCRIPT_OR_STYLE.sub("", markup)
    with_boundaries = _BLOCK.sub("\n", without_invisible)
    visible = _decode_entities(_TAG.sub("", with_boundaries))
    lines = [
        _INLINE_SPACE.sub(" ", line).strip()
        for line in visible.replace("\r", "").split("\n")
    ]
    # 丢弃空行，再用**双换行**连接：单换行在 eConfirm 正文里是同一字段的续行。
    return "\n\n".join(line for line in lines if line)


def normalize_email_body_candidates(
    parts: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """按「纯文本优先，其次 HTML」的顺序归一并去重正文候选。

    @param parts: ``(mime_type, content)`` 序列。
    @returns 去重后的候选正文，顺序稳定。
    @raises ValueError: 不存在的 MIME 类型会被忽略（不报错）。
    """
    candidates: list[str] = []
    for expected in ("text/plain", "text/html"):
        for mime_type, content in parts:
            if mime_type != expected:
                continue
            candidate = html_to_text(content) if mime_type == "text/html" else content
            # 空正文与重复正文都不作为候选：前者无信息，后者会让解析器重复处理。
            if candidate.strip() and candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def _decode_entities(value: str) -> str:
    """解码 eConfirm 字段可能使用的标准 HTML 实体。

    未知的命名实体**原样保留**——猜它可能是哪个字符会让正文内容被篡改。
    """
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token[:2].lower() == "#x":
            try:
                return chr(int(token[2:], 16))
            except (ValueError, OverflowError):
                return match.group(0)
        if token.startswith("#"):
            try:
                return chr(int(token[1:], 10))
            except (ValueError, OverflowError):
                return match.group(0)
        return _NAMED_ENTITIES.get(token.lower(), match.group(0))

    return _ENTITY.sub(replace, value)

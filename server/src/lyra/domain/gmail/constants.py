"""Schwab 通知邮件的固定标识。

镜像 TypeScript 版 ``domain/gmail/econfirm.ts`` 的常量部分。

这两项是**搜索的锚点**：只有来自该发件人且主题为该字符串的邮件才会被解析。
放宽任何一项都会让无关邮件进入账本解析流程。
"""

from __future__ import annotations

SCHWAB_ECONFIRM_SENDER = "donotreply@mail.schwab.com"
SCHWAB_ECONFIRM_SUBJECT = "Schwab eConfirms"

"""应用层错误契约。

镜像 TypeScript 版 ``app-error.ts``。``code`` 是 App 可以稳定分支处理的值；
``message`` 面向用户且不得含令牌、验证码或第三方凭据；``details`` 只在明确
安全时携带结构化补充信息。
"""

from __future__ import annotations

from typing import Any


class ApplicationError(Exception):
    """带稳定错误码与 HTTP 状态的应用层错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details

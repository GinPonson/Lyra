"""Gmail 与 Google OAuth 的真实 HTTP 传输层。

前面的模块都以可注入回调的形式接收传输层，因此整条链路可以在没有网络的情况下完整
测试；本模块只负责把 httpx 接上去。

**四道硬约束**（与行情 HTTP 客户端同一条原则，§85.1）：

| 约束 | 不遵守会怎样 |
| --- | --- |
| **禁止重定向** | 跟随即等于把 access token 发给重定向目标 |
| **超时含正文读取** | 慢速上游能把连接挂住 |
| **正文体积上限** | 把内存上限交给上游 |
| **不回传供应商正文** | 错误正文常含端点、令牌片段或内部堆栈 |

**令牌只进 Authorization 头**，不进 URL 或日志。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
import re
from typing import Any

# 单次响应上限：Gmail 的消息正文可能较大，但仍要有界。
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


_OAUTH_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class GoogleEndpointFailure(Exception):
    """Google HTTP 调用失败，只保留状态与受限错误码，不保存响应正文。

    @param status: HTTP 状态；``None`` 表示请求未收到 HTTP 响应。
    @param retryable: 网络错误、429 或 5xx 是否可重试。
    @param oauth_error_code: Google 返回的稳定错误码；不含 ``error_description``。
    """

    def __init__(
        self,
        status: int | None,
        retryable: bool,
        oauth_error_code: str | None = None,
    ) -> None:
        super().__init__(f"Google endpoint failed with status {status}")
        self.status = status
        self.retryable = retryable
        self.oauth_error_code = oauth_error_code


def build_form_getter(
    timeout_seconds: float = 15.0, *, transport: Any | None = None
) -> Any:
    """构造 OAuth 表单 POST 传输层，成功时返回 JSON、失败时抛安全异常。

    @param timeout_seconds: 读超时。
    @param transport: 可选 HTTPX transport；生产使用默认网络传输，测试可传 MockTransport。
    @returns ``(url, form, timeout) -> payload``；非 2xx 与网络失败映射为异常。
    """
    import httpx

    def post_form(
        url: str, form: Mapping[str, str], timeout: float | None = None
    ) -> Any:
        """提交表单，拒绝 HTTP 错误且不向异常暴露供应商正文。

        @raises GoogleEndpointFailure: 非 2xx 或 HTTPX 网络/超时失败。
        """
        try:
            with httpx.Client(
                timeout=timeout or timeout_seconds,
                follow_redirects=False,
                transport=transport,
            ) as client:
                response = client.post(
                    url, data=dict(form),
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError:
            # 网络异常的原文可能包含 request details；只保留安全的重试分类。
            raise GoogleEndpointFailure(None, retryable=True) from None
        payload = _parse(response)
        if not 200 <= response.status_code < 300:
            raise _response_failure(response.status_code, payload) from None
        return payload

    return post_form


def build_json_getter(
    timeout_seconds: float = 15.0, *, transport: Any | None = None
) -> Any:
    """构造带认证头的 JSON GET 传输层。

    @param timeout_seconds: 读超时。
    @param transport: 可选 HTTPX transport；生产使用默认网络传输，测试可传 MockTransport。
    @returns ``(url, headers, timeout) -> (status, payload)``。
    """
    import httpx

    def get_json(
        url: str, headers: Mapping[str, str], timeout: float | None = None
    ) -> tuple[int, Any]:
        """发起 GET 并返回状态与 JSON。

        **不跟随重定向**：跟随即等于把 access token 发给重定向目标。
        """
        with httpx.Client(
            timeout=timeout or timeout_seconds,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.get(url, headers=dict(headers))
            return response.status_code, _parse(response)

    return get_json


def build_oauth_json_getter(
    timeout_seconds: float = 15.0, *, transport: Any | None = None
) -> Any:
    """构造 OAuth JSON GET 传输层，成功返回对象、失败抛安全端点异常。

    Gmail API 使用 :func:`build_json_getter` 的显式状态码合同；OAuth userinfo 则与
    token client 一样使用 payload-or-error，避免把 401 错误 JSON 当作身份对象解析。

    @param timeout_seconds: 读超时。
    @param transport: 可选 HTTPX transport；生产使用默认网络传输，测试可传 MockTransport。
    @returns ``(url, headers, timeout) -> payload``。
    @raises GoogleEndpointFailure: 非 2xx 或 HTTPX 网络/超时失败。
    """
    import httpx

    def get_oauth_json(
        url: str, headers: Mapping[str, str], timeout: float | None = None
    ) -> Any:
        """发起 userinfo GET，并拒绝 HTTP 错误。"""
        try:
            with httpx.Client(
                timeout=timeout or timeout_seconds,
                follow_redirects=False,
                transport=transport,
            ) as client:
                response = client.get(url, headers=dict(headers))
        except httpx.HTTPError:
            raise GoogleEndpointFailure(None, retryable=True) from None
        payload = _parse(response)
        if not 200 <= response.status_code < 300:
            raise _response_failure(response.status_code, payload) from None
        return payload

    return get_oauth_json


def _response_failure(status: int, payload: Any) -> GoogleEndpointFailure:
    """从非 2xx 响应提取可安全记录的 OAuth 错误码。"""
    code = payload.get("error") if isinstance(payload, dict) else None
    safe_code = code if isinstance(code, str) and _OAUTH_ERROR_CODE.fullmatch(code) else None
    return GoogleEndpointFailure(
        status=status,
        retryable=status == 429 or status >= 500,
        oauth_error_code=safe_code,
    )


def _parse(response: Any) -> Any:
    """按上限读取并解析响应正文。

    **错误响应同样要读**：调用方需要状态码，但正文只用于诊断，绝不回传给客户端。

    @returns 解析后的 JSON；正文为空或非法时为 ``None``。
    """
    # 声明长度超限时直接放弃读取，避免上游用声明值骗过内存限制。
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                return None
        except ValueError:
            return None
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

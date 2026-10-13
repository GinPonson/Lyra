"""行情 HTTP 客户端测试。

最要紧的两条：

1. **403 归为"来源不可用"，不是"凭据被拒"**——这些供应商的 403 通常表示账号缺少
   SIP / OPRA 权限。归错会让用户去轮换一把**其实有效**的密钥；
2. **正文体积双检**：只信 `Content-Length` 等于把内存上限交给上游。
"""

from __future__ import annotations

import json

from lyra.infrastructure.market_data.http import (
    MARKET_DATA_MAX_BODY_BYTES,
    MarketJsonError,
    MarketJsonOk,
    request_market_json,
)


class FakeResponse:
    """可脚本化的响应。"""

    def __init__(
        self, *, status: int = 200, body: bytes = b"{}",
        headers: dict | None = None, chunks=None, raise_after: int | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self._chunks = chunks
        self._raise_after = raise_after

    def chunks(self):
        if self._chunks is not None:
            yield from self._chunks
            return
        if self._raise_after is not None:
            yield self._body[: self._raise_after]
            raise TimeoutError("body read timed out")
        yield self._body


def _request(response=None, *, error=None, headers=None, **kwargs):
    def fetch(url, *, headers, timeout):
        if error is not None:
            raise error
        return response

    return request_market_json(
        url="https://data.example.com/quotes", headers=headers or {"x-api-key": "k"},
        fetch=fetch, **kwargs,
    )


# --------------------------------------------------------------------------
# 成功
# --------------------------------------------------------------------------


def test_parses_a_successful_body() -> None:
    """2xx 解析正文。"""
    result = _request(FakeResponse(body=json.dumps({"quote": 12.5}).encode()))

    assert isinstance(result, MarketJsonOk)
    assert result.body == {"quote": 12.5}
    assert result.status == 200


def test_404_is_not_a_transport_failure() -> None:
    """**404 不算失败**：它由供应商适配器解释为"无数据"。"""
    result = _request(FakeResponse(status=404, body=b'{"message":"not found"}'))

    assert isinstance(result, MarketJsonOk)
    assert result.status == 404


def test_203_is_treated_as_success() -> None:
    """203 同样通过。"""
    assert isinstance(_request(FakeResponse(status=203)), MarketJsonOk)


# --------------------------------------------------------------------------
# 状态码映射
# --------------------------------------------------------------------------


def test_401_means_credentials_rejected() -> None:
    """**401 才是密钥无效。**"""
    result = _request(FakeResponse(status=401))
    assert isinstance(result, MarketJsonError)
    assert result.reason == "credentials_rejected"


def test_403_means_provider_unavailable_not_bad_credentials() -> None:
    """**403 归为来源不可用。**

    这些供应商的 403 通常表示账号缺少 SIP / OPRA 行情权限。归为"凭据被拒"会让用户
    去轮换一把**其实有效**的密钥，然后问题依旧。
    """
    result = _request(FakeResponse(status=403))

    assert isinstance(result, MarketJsonError)
    assert result.reason == "provider_unavailable"
    assert result.reason != "credentials_rejected"


def test_429_is_rate_limited_with_retry_after() -> None:
    """429 映射为限流，并读出 Retry-After。"""
    result = _request(FakeResponse(status=429, headers={"retry-after": "30"}))

    assert isinstance(result, MarketJsonError)
    assert result.reason == "rate_limited"
    assert result.retry_after_seconds == 30


def test_invalid_retry_after_is_ignored() -> None:
    """无法解析的 Retry-After 返回 None，**不猜**。"""
    result = _request(FakeResponse(status=429, headers={"retry-after": "Wed, 21 Oct"}))
    assert isinstance(result, MarketJsonError)
    assert result.retry_after_seconds is None


def test_500_is_provider_unavailable() -> None:
    """5xx 归为来源不可用。"""
    result = _request(FakeResponse(status=500))
    assert isinstance(result, MarketJsonError)
    assert result.reason == "provider_unavailable"


# --------------------------------------------------------------------------
# 传输失败
# --------------------------------------------------------------------------


def test_timeout_maps_to_timeout() -> None:
    """超时映射为 timeout。"""
    result = _request(error=TimeoutError("connect timed out"))
    assert isinstance(result, MarketJsonError)
    assert result.reason == "timeout"


def test_transport_error_does_not_leak_details() -> None:
    """**传输异常细节不得回传**——它常含端点与密钥片段。"""
    result = _request(error=RuntimeError("api key sk-secret rejected at https://internal"))

    assert isinstance(result, MarketJsonError)
    assert "sk-secret" not in str(result)
    assert "internal" not in str(result)


def test_body_read_timeout_maps_to_timeout() -> None:
    """**超时包含正文读取**：读到一半超时也算 timeout。"""
    result = _request(FakeResponse(body=b"x" * 100, raise_after=10))

    assert isinstance(result, MarketJsonError)
    assert result.reason == "timeout"


# --------------------------------------------------------------------------
# 正文上限
# --------------------------------------------------------------------------


def test_oversized_declared_length_is_rejected() -> None:
    """声明长度超限即拒绝，不读取正文。"""
    result = _request(FakeResponse(
        body=b"{}", headers={"content-length": str(MARKET_DATA_MAX_BODY_BYTES + 1)}
    ))

    assert isinstance(result, MarketJsonError)
    assert result.reason == "invalid_response"


def test_oversized_actual_body_is_rejected() -> None:
    """**实际字节数超限也必须拒绝**——不能只信声明头。"""
    result = _request(FakeResponse(chunks=[b"x" * (MARKET_DATA_MAX_BODY_BYTES + 1)]))

    assert isinstance(result, MarketJsonError)
    assert result.reason == "invalid_response"


def test_body_at_the_limit_is_accepted() -> None:
    """恰好等于上限通过。"""
    payload = json.dumps({"x": "y" * (MARKET_DATA_MAX_BODY_BYTES - 20)}).encode()
    assert len(payload) <= MARKET_DATA_MAX_BODY_BYTES

    result = _request(FakeResponse(body=payload, headers={
        "content-length": str(len(payload))
    }))
    assert isinstance(result, MarketJsonOk)


def test_malformed_content_length_is_rejected() -> None:
    """无法解析的声明长度视为非法响应。"""
    result = _request(FakeResponse(headers={"content-length": "not-a-number"}))
    assert isinstance(result, MarketJsonError)
    assert result.reason == "invalid_response"


# --------------------------------------------------------------------------
# 正文解析
# --------------------------------------------------------------------------


def test_invalid_json_is_rejected() -> None:
    """非 JSON 正文必须拒绝。"""
    result = _request(FakeResponse(body=b"not json"))

    assert isinstance(result, MarketJsonError)
    assert result.reason == "invalid_response"


def test_non_utf8_body_is_rejected() -> None:
    """非 UTF-8 正文必须拒绝。"""
    result = _request(FakeResponse(body=b"\xff\xfe"))

    assert isinstance(result, MarketJsonError)
    assert result.reason == "invalid_response"


def test_error_results_carry_no_body() -> None:
    """错误结果不携带任何供应商正文。"""
    result = _request(FakeResponse(status=401, body=b'{"secret":"leaked"}'))

    assert isinstance(result, MarketJsonError)
    assert not hasattr(result, "body")
    assert "leaked" not in str(result)

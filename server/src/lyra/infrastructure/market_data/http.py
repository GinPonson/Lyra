"""行情 HTTP 客户端。

镜像 TypeScript 版 ``infrastructure/market-data/http.ts``。

五条硬约束，每条都对应一种真实故障：

| 约束 | 不遵守会怎样 |
| --- | --- |
| **禁止重定向** | 跟随即等于把 API 密钥发给重定向目标 |
| **超时含正文读取** | 只约束建连时间，一个慢速上游能把连接挂住直到天荒地老 |
| **正文体积双检**（Content-Length + 实际累计） | 只信声明头等于把内存上限交给上游 |
| **不把供应商正文或传输异常回传** | 错误正文常含端点、密钥片段或内部堆栈 |
| **403 归为"来源不可用"** | 归为"凭据被拒"会让用户去轮换一把**其实有效**的密钥 |

最后一条尤其容易做错：这些供应商的 403 通常表示账号缺少 SIP / OPRA 行情权限，
不是密钥问题。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ...domain.market_data.types import MarketQuoteIssue

MARKET_DATA_TIMEOUT_MILLISECONDS = 10_000
MARKET_DATA_MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class MarketJsonOk:
    """成功读取的 JSON 响应。"""

    ok: Literal[True]
    status: int
    body: Any
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class MarketJsonError:
    """失败，只带稳定原因码。"""

    ok: Literal[False]
    reason: MarketQuoteIssue
    status: int | None = None
    retry_after_seconds: int | None = None


MarketJsonResult = MarketJsonOk | MarketJsonError


class MarketResponse(Protocol):
    """传输层返回的响应；**正文按块读取**，由本模块执行体积上限。"""

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def chunks(self) -> Iterable[bytes]: ...


# 注入的传输层：接收 URL、请求头与超时（秒），返回响应。
MarketDataFetch = Callable[..., MarketResponse]


def request_market_json(
    *,
    url: str,
    headers: Mapping[str, str],
    fetch: MarketDataFetch,
    timeout_ms: int = MARKET_DATA_TIMEOUT_MILLISECONDS,
    max_body_bytes: int = MARKET_DATA_MAX_BODY_BYTES,
) -> MarketJsonResult:
    """发起一次行情请求并解析 JSON。

    @param url: 完整地址。
    @param headers: 请求头（密钥只在这里）。
    @param fetch: 传输层。
    @param timeout_ms: 超时（含正文读取）。
    @param max_body_bytes: 正文上限。
    @returns 成功结果或稳定原因码。
    """
    try:
        response = fetch(url, headers=dict(headers), timeout=timeout_ms / 1000)
    except TimeoutError:
        return MarketJsonError(ok=False, reason="timeout")
    except Exception:  # noqa: BLE001 - 传输异常一律折叠，细节只进日志
        return MarketJsonError(ok=False, reason="provider_unavailable")

    failure = _http_failure(response.status)
    if failure is not None:
        return MarketJsonError(
            ok=False, reason=failure, status=response.status,
            retry_after_seconds=_retry_after(response.headers),
        )

    try:
        body = _read_bounded_json(
            response, max_body_bytes=max_body_bytes
        )
    except _InvalidMarketPayload:
        return MarketJsonError(ok=False, reason="invalid_response", status=response.status)
    except TimeoutError:
        return MarketJsonError(ok=False, reason="timeout")
    except Exception:  # noqa: BLE001
        return MarketJsonError(ok=False, reason="provider_unavailable")

    return MarketJsonOk(
        ok=True, status=response.status, body=body,
        retry_after_seconds=_retry_after(response.headers),
    )


class _InvalidMarketPayload(Exception):
    """响应体不符合预期（体积超限或不是 JSON）。"""


def _http_failure(status: int) -> MarketQuoteIssue | None:
    """把 HTTP 状态映射为稳定原因码。

    **401 才是密钥无效；403 归为来源不可用。** 这些供应商的 403 通常表示账号缺少
    SIP / OPRA 权限——归为凭据问题会让用户去轮换一把其实有效的密钥。

    200 / 203 / 404 不算失败：404 由供应商适配器解释为"无数据"。

    @param status: HTTP 状态码。
    @returns 失败原因；非失败返回 ``None``。
    """
    if status == 401:
        return "credentials_rejected"
    if status == 403:
        return "provider_unavailable"
    if status == 429:
        return "rate_limited"
    if status in (200, 203, 404):
        return None
    return "provider_unavailable"


def _read_bounded_json(response: MarketResponse, *, max_body_bytes: int) -> Any:
    """按上限读取并解析 JSON。

    先看 ``Content-Length``，再累计**实际**读到的字节数——只信前者等于把内存上限
    交给上游。

    @raises _InvalidMarketPayload: 体积超限或不是合法 JSON。
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_body_bytes:
                raise _InvalidMarketPayload()
        except ValueError as error:
            raise _InvalidMarketPayload() from error

    chunks: list[bytes] = []
    total = 0
    for chunk in response.chunks():
        total += len(chunk)
        if total > max_body_bytes:
            raise _InvalidMarketPayload()
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _InvalidMarketPayload() from error


def _retry_after(headers: Mapping[str, str]) -> int | None:
    """读取 ``Retry-After``（仅支持秒数形式，日期形式忽略）。

    只做保守解释：解析不出就返回 ``None``，不猜。
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    if value < 0 or not math.isfinite(value):
        return None
    return value

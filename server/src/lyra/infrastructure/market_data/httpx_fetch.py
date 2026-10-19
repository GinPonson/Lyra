"""行情 HTTP 传输层：把 httpx 接到 ``MarketDataFetch`` 上。

`application/market_data_service` 与两个供应商适配器都以可注入回调的形式接收传输层，
因此整条链路可以在没有网络的情况下完整测试；本模块只负责接线。

**两道与行情客户端同源的约束**（§85.1）：

- **禁止重定向**——跟随即等于把 API 密钥发给重定向目标；
- **流式读取正文**——体积上限由上层按块累计执行；一次性读进内存等于把上限交给上游。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class HttpxMarketResponse:
    """httpx 响应的适配器。

    正文**按块产出**，不预先读进内存。
    """

    def __init__(self, response: Any) -> None:
        self._response = response

    @property
    def status(self) -> int:
        """HTTP 状态码。"""
        return int(self._response.status_code)

    @property
    def headers(self) -> Mapping[str, str]:
        """响应头（小写键，与 httpx 一致）。"""
        return self._response.headers

    def chunks(self) -> Iterable[bytes]:
        """按块产出正文。"""
        return self._response.iter_bytes()


def httpx_market_fetch(
    url: str, *, headers: Mapping[str, str], timeout: float
) -> HttpxMarketResponse:
    """发起一次行情请求。

    **不跟随重定向**；超时同时覆盖建连与正文读取。

    @param url: 完整地址。
    @param headers: 请求头（密钥只在这里）。
    @param timeout: 超时（秒）。
    @returns 响应适配器。
    """
    import httpx

    # 用 Client 而不是顶层 `get`：需要在流式读取期间保持连接与超时设置。
    client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        request = client.build_request("GET", url, headers=dict(headers))
        response = client.send(request, stream=True)
    except Exception:
        client.close()
        raise
    return _ClosingResponse(client, response)


class _ClosingResponse(HttpxMarketResponse):
    """在正文读完或出错后关闭客户端的响应。"""

    def __init__(self, client: Any, response: Any) -> None:
        super().__init__(response)
        self._client = client

    def chunks(self) -> Iterable[bytes]:
        """按块产出正文，结束后关闭连接。"""
        try:
            for chunk in self._response.iter_bytes():
                yield chunk
        finally:
            self._response.close()
            self._client.close()

"""行情 HTTP 传输层适配器测试。

只测适配器本身的行为（状态、头、分块、关闭），不打网络。
"""

from __future__ import annotations

from lyra.infrastructure.market_data.httpx_fetch import _ClosingResponse


class _FakeResponse:
    """httpx 响应的最小桩。"""

    def __init__(self, *, status_code: int = 200, chunks=(b"a", b"b")) -> None:
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._chunks = chunks
        self.closed = False

    def iter_bytes(self):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """httpx 客户端的最小桩。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_status_and_headers_pass_through() -> None:
    """状态码与响应头透传。"""
    response = _ClosingResponse(_FakeClient(), _FakeResponse(status_code=404))

    assert response.status == 404
    assert response.headers["content-type"] == "application/json"


def test_chunks_yield_the_body_in_order() -> None:
    """正文按块产出。"""
    response = _ClosingResponse(_FakeClient(), _FakeResponse(chunks=(b"x", b"y", b"z")))

    assert list(response.chunks()) == [b"x", b"y", b"z"]


def test_both_response_and_client_are_closed_after_reading() -> None:
    """**读完正文后响应与客户端都被关闭。**

    不关会泄漏连接——长时间运行的服务会把文件描述符耗尽，而症状是"偶发请求失败"。
    """
    inner = _FakeResponse()
    client = _FakeClient()
    response = _ClosingResponse(client, inner)

    list(response.chunks())

    assert inner.closed is True
    assert client.closed is True


def test_connections_are_closed_even_when_reading_fails() -> None:
    """**读取中途出错同样要关闭。**

    只在上层按块累计体积，超限时会提前中止迭代；那条路径也必须释放连接。
    """
    class _Exploding(_FakeResponse):
        def iter_bytes(self):
            yield b"partial"
            raise RuntimeError("connection reset")

    inner = _Exploding()
    client = _FakeClient()
    response = _ClosingResponse(client, inner)

    try:
        list(response.chunks())
    except RuntimeError:
        pass

    assert inner.closed is True
    assert client.closed is True

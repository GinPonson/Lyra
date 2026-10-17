"""启动后自检：确认正在运行的 Python 服务真的可用。

**为什么单独一步**：进程"起来了"与"能服务"是两件事。uvicorn 打印 "Application startup
complete" 之后，服务仍可能因为数据库不可读、主密钥缺失或路由装配不全而无法真正工作。

这个脚本按运行手册阶段 2 的检查表逐项验证，**任一项失败即非零退出**。

用法：

    cd server
    UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \\
      uv run --python 3.12 python scripts/verify_running_service.py \\
      --base-url http://127.0.0.1:3141 [--device-token "$DEVICE_TOKEN"]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lyra.db.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402
from lyra.protocol import (  # noqa: E402
    CLIENT_PROTOCOL_HEADERS,
    LYRA_PROTOCOL_VERSION,
)

#: **期望值必须与生产代码同源，不能在这里写第二份字面量。**
#:
#: 这两行曾是硬编码的 ``2`` 与 ``15``。协议升到 3、schema 升到 16 之后没有任何
#: 东西提醒更新它们，结果是一个**完全正确**的部署被自检判为未通过：
#: "schema_version=16（期望 15）/ protocol_version=3（期望 2）"，并打印
#: 「不要继续切换」。部署当天最不该出现的信号，就是把成功报成失败。
EXPECTED_PROTOCOL_VERSION = LYRA_PROTOCOL_VERSION
EXPECTED_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION


#: 客户端必须随每个 ``/v1`` 请求带的协议版本头。**与生产代码同源**，
#: 避免这里写第二份字面量。
PROTOCOL_HEADERS = dict(CLIENT_PROTOCOL_HEADERS)


def _get(
    url: str, token: str | None = None, extra_headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    """发 GET 请求并返回 (状态码, 解析后的正文或原始文本)。

    @param url: 完整 URL。
    @param token: 可选的设备访问令牌。
    @returns 状态码与正文。
    """
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    for name, value in (extra_headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        status = error.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def main() -> int:
    """跑完全部启动后检查。"""
    parser = argparse.ArgumentParser(description="验证正在运行的服务")
    parser.add_argument("--base-url", required=True, help="服务基址，如 http://127.0.0.1:3141")
    parser.add_argument("--device-token", default=None, help="设备访问令牌（可选）")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    failures: list[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        print(f"[{'通过' if passed else '未通过'}] {name}: {detail}")
        if not passed:
            failures.append(name)

    # 1) 健康端点。**degraded 也算通过**——它表示服务能工作、只是部分能力受损；
    #    把它当失败会让一个可用的服务被误判为不可用。
    status, body = _get(f"{base}/v1/health")
    healthy = status == 200 and isinstance(body, dict) and body.get("status") in (
        "healthy", "degraded",
    )
    detail = (
        f"{status} / status={body.get('status')}"
        if isinstance(body, dict) else f"{status} / (正文非 JSON)"
    )
    check("/v1/health 可用", healthy, detail)
    if isinstance(body, dict) and body.get("status") == "degraded":
        print(f"       └─ degraded 的原因：gmail={body.get('gmail')} agents={body.get('agents')}")

    # 2) 协议元数据：**schema 版本与协议版本必须与预期一致**。
    status, body = _get(f"{base}/v1/system/info")
    if status == 200 and isinstance(body, dict):
        schema_ok = body.get("schema_version") == EXPECTED_SCHEMA_VERSION
        protocol_ok = body.get("protocol_version") == EXPECTED_PROTOCOL_VERSION
        check(
            "/v1/system/info 模式版本",
            schema_ok,
            f"schema_version={body.get('schema_version')}（期望 {EXPECTED_SCHEMA_VERSION}）",
        )
        check(
            "/v1/system/info 协议版本",
            protocol_ok,
            f"protocol_version={body.get('protocol_version')}（期望 {EXPECTED_PROTOCOL_VERSION}）",
        )
    else:
        check("/v1/system/info", False, f"{status}")

    # 3) **客户端协议门**：缺少协议头的请求必须是 426，不是 401。
    #
    #    两者的区别对客户端有意义：401 说"凭据不对"（客户端会去刷新令牌），
    #    426 说"App 版本不兼容"（唯一正确的动作是提示升级）。
    status, body = _get(f"{base}/v1/ledger/accounts")
    gate_ok = (
        status == 426
        and isinstance(body, dict)
        and body.get("code") == "CLIENT_VERSION_INCOMPATIBLE"
    )
    check(
        "缺少协议头返回 426（而非 401）",
        gate_ok,
        f"{status} / code={body.get('code') if isinstance(body, dict) else '—'}",
    )
    if isinstance(body, dict) and isinstance(body.get("details"), dict):
        print(f"       └─ 支持区间：{body['details']}")

    # 4) 未认证的受保护端点必须是 401，**且错误体是扁平信封**。
    #
    #    这条同时验证了两件事：认证在生效，以及错误响应形状正确——后者是客户端错误
    #    处理逻辑的依据。
    status, body = _get(f"{base}/v1/ledger/summary", None, PROTOCOL_HEADERS)
    envelope_ok = (
        status == 401
        and isinstance(body, dict)
        and {"code", "message", "retryable", "request_id"} <= set(body)
    )
    check("受保护端点要求认证且信封扁平", envelope_ok, f"{status} / 键={sorted(body) if isinstance(body, dict) else '—'}")

    # 5) 带令牌时读一次真实账本——**这是"能服务"的唯一直接证据**。
    if args.device_token:
        status, body = _get(f"{base}/v1/ledger/summary", args.device_token, PROTOCOL_HEADERS)
        check(
            "带令牌读取账本",
            status == 200,
            f"{status}" if status == 200 else f"{status} / {body}",
        )
        status, body = _get(
            f"{base}/v1/ledger/summary?scope=all", args.device_token, PROTOCOL_HEADERS
        )
        check("?scope=all 可读", status == 200, f"{status}")
        status, _ = _get(
            f"{base}/v1/ledger/summary?scope=nope", args.device_token, PROTOCOL_HEADERS
        )
        check("非法 scope 报 400（而非静默全部）", status == 400, f"{status}")
    else:
        print("[跳过] 带令牌的账本读取——未提供 --device-token")

    print("-" * 68)
    if failures:
        print(f"{len(failures)} 项未通过：{', '.join(failures)}")
        print("**不要继续切换。**")
        return 1
    print("正在运行的服务已通过全部检查。")
    if not args.device_token:
        print("注意：未提供 --device-token，因此**没有验证过真实账本读取**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

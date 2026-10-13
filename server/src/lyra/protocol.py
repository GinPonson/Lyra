"""服务端协议版本与公开元数据。

镜像 TypeScript 版 ``protocol-version.ts``。协议版本是客户端与服务端之间的
硬契约：迁移期间**不得变更**，否则 Flutter 端需要同步改动。
"""

from __future__ import annotations

from typing import TypedDict

# 当前移动端与服务端共享的协议版本。
LYRA_PROTOCOL_VERSION = 2
# 当前服务端接受的最旧协议版本。
MIN_SUPPORTED_PROTOCOL_VERSION = 2
# 当前服务端接受的最新协议版本。
MAX_SUPPORTED_PROTOCOL_VERSION = LYRA_PROTOCOL_VERSION


class ServerProtocolMetadata(TypedDict):
    """``/v1/system/info`` 可直接返回的无敏感协议元数据。"""

    server_version: str
    protocol_version: int
    min_supported_protocol_version: int
    max_supported_protocol_version: int
    schema_version: int


def create_server_protocol_metadata(server_version: str, schema_version: int) -> ServerProtocolMetadata:
    """构造公开的服务端协议元数据。

    @param server_version: 服务端发布版本，不含构建路径或提交环境信息。
    @param schema_version: 当前已经完成的 DuckDB migration 版本。
    @returns 可直接序列化的协议元数据。
    @throws ValueError: 版本为空，或 schema 版本不是非负整数。
    """
    if not server_version.strip():
        raise ValueError("serverVersion 不能为空")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 0:
        raise ValueError("schemaVersion 必须是非负整数")
    return {
        "server_version": server_version,
        "protocol_version": LYRA_PROTOCOL_VERSION,
        "min_supported_protocol_version": MIN_SUPPORTED_PROTOCOL_VERSION,
        "max_supported_protocol_version": MAX_SUPPORTED_PROTOCOL_VERSION,
        "schema_version": schema_version,
    }


#: 客户端必须随每个 ``/v1`` 请求发送的协议版本头。
#:
#: 放在生产代码里而不是测试里：**它是协议的一部分**，客户端的 HTTP 层要用它，
#: 测试与部署探针也要用它。写在测试里会让三者各有一份字面量。
CLIENT_PROTOCOL_HEADER = "x-lyra-protocol-version"
CLIENT_PROTOCOL_HEADERS = {CLIENT_PROTOCOL_HEADER: str(LYRA_PROTOCOL_VERSION)}

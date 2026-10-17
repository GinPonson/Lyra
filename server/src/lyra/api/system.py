"""系统信息端点。

镜像 TypeScript 版 ``/v1/system/info``：只公开协议元数据，不暴露构建路径、
提交信息或任何环境细节。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..db.connection import LyraDatabase
from ..db.migrations import CURRENT_SCHEMA_VERSION
from ..protocol import create_server_protocol_metadata

# 服务端发布版本；与 TypeScript 版同样只表示发布号，不含构建环境信息。
SERVER_VERSION = "0.1.0"


def create_system_router(database: LyraDatabase) -> APIRouter:
    """构造系统信息路由。

    @param database: 已打开的账本连接，用于读取当前 schema 版本。
    @returns 挂载 ``GET /v1/system/info`` 的路由器。
    """
    router = APIRouter()

    @router.get("/v1/system/info")
    def read_system_info() -> dict[str, object]:
        """返回公开协议元数据。

        **``schema_version`` 是本二进制编译期已知的常量，不读数据库。**

        它此前从健康快照读取、数据库不可用时回退为 **0**——而客户端正是用这个端点判断
        协议与模式兼容性。``0`` 不是任何有效的模式版本，会让客户端误判（例如认为服务端
        模式过旧而拒绝连接），而真实原因只是**数据库暂时不可用**。

        参考实现同样用编译期常量（``CURRENT_SCHEMA_VERSION``），与运行期状态无关。

        @returns 协议版本区间与当前 schema 版本。
        """
        return dict(create_server_protocol_metadata(SERVER_VERSION, CURRENT_SCHEMA_VERSION))

    return router

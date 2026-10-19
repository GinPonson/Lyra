"""健康端点。

响应形状对齐 TypeScript 版 ``/v1/health``，含七段：``status`` / ``checkedAt`` /
``process`` / ``database`` / ``writeQueue`` / ``gmail`` / ``agents``。

**总体状态的判定规则**（与参考实现一致）：

```
unavailable  ← 数据库不可用 或 写队列不接受
degraded     ← Gmail 有需关注的连接 或 Agent 不可用
healthy      ← 其余
```

``degraded`` 仍是 **HTTP 200**——服务能工作，只是有部分能力受损。把 degraded 报成
503 会让客户端直接放弃一个其实可用的服务。

各段缺失时**不填占位值**：健康端点谎报可用会让故障排查建立在假信息上。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response

from ..db.connection import LyraDatabase


def _iso_timestamp() -> str:
    """返回与 JavaScript ``toISOString()`` 同形的 UTC 时间戳。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")



def create_health_router(
    database: LyraDatabase,
    write_coordinator_provider: Any = None,
    gmail_attention_provider: Any = None,
    agent_availability: Any = None,
) -> APIRouter:
    """构造健康路由。

    三个可选参数对应三段健康信息。**未提供时该段不出现**，而不是报一个假的
    ``available``——健康端点谎报可用会让故障排查建立在假信息上。

    **Gmail 段只报状态，不报数量**（计划 §4.4、P0 契约 §3.4）：连接数与待处理数在
    多用户下是**用户活动信息**，公开端点不得暴露。因此这里的入参是一个返回
    「需要处理的连接数」的部署级聚合函数，而不是连接列表——列表会诱导调用方把
    长度和明细一起写进响应。

    @param database: 已打开的账本连接。
    @param write_coordinator_provider: 返回写队列协调器的可调用对象；``None`` 表示未装配该段。
    @param gmail_attention_provider: 返回「部署级需要处理的 Gmail 连接数」的可调用对象；
        ``None`` 表示未装配该段。
    @param agent_availability: 返回 Agent 可用性快照的可调用对象；返回 ``None`` 表示
        运行时缺席（不知道是否可用）。``None`` 参数表示未装配该段。
    @returns 挂载 ``GET /v1/health`` 的路由器。
    """
    router = APIRouter()

    @router.get("/v1/health")
    def read_health(response: Response) -> dict[str, object]:
        """返回不含个人信息与内部错误的服务健康快照。

        @param response: FastAPI 响应对象，用于在数据库不可用时返回 503。
        @returns 健康快照；数据库不可用时为 ``unavailable``。
        """
        database_health = database.read_health()

        snapshot: dict[str, object] = {
            "checkedAt": _iso_timestamp(),
            "process": {"status": "available"},
            "database": database_health,
        }

        accepting = True
        if write_coordinator_provider is not None:
            write_coordinator = write_coordinator_provider()
            accepting = write_coordinator.accepting
            snapshot["writeQueue"] = {
                "status": "available" if accepting else "draining",
                "pendingOperations": write_coordinator.pending_operations,
            }

        attention_required = 0
        if gmail_attention_provider is not None:
            # **读 Gmail 状态失败不应让健康端点本身失败，也不应把整体拖成降级。**
            #
            # 数据库已关闭时这次读取会抛异常，而健康端点的职责恰恰是**报告**不可用，
            # 不是跟着一起坏掉——否则监控看到的是"健康检查 500"，而不是
            # "服务不可用"，两者的排查方向完全不同。
            #
            # 读取失败时该段不出现（而不是填一个假的 available），且 `attention_required`
            # 保持 0：**"读不到"不等于"有连接需要处理"**，把它算成降级会让一次探测故障
            # 伪装成 Gmail 出问题。
            probed: int | None
            try:
                probed = int(gmail_attention_provider())
            except Exception:  # noqa: BLE001 - 健康端点必须始终能回应
                probed = None
            if probed is not None:
                attention_required = probed
                # **只报状态**：``totalConnections`` 与 ``attentionRequired`` 在多用户下
                # 是用户活动计数，公开端点不得暴露（计划 §4.4）。需要这些数字的运维查询
                # 走本地库，不走公开接口。
                snapshot["gmail"] = {
                    "status": "degraded" if attention_required > 0 else "available",
                }


        agent_status = "unknown"
        if agent_availability is not None:
            availability = agent_availability()
            # **运行时缺席时状态是 ``unknown``，不是 ``degraded``。**
            #
            # 参考实现里 ``agentAvailability === null``（没有运行时）映射为 ``unknown``：
            # 我们**不知道**它是否可用，而不是知道它坏了。"不知道"与"坏了"对排查者
            # 是不同的信息。
            #
            # 但两者都会让整体变成 ``degraded``——因为判定是 `status !== "available"`。
            if availability is None:
                available_providers = 0
                unavailable_providers = 0
            else:
                # **计数由真实 provider 列表推导**，不是"配置了就算 1 个可用"。
                #
                # 参考实现的可用性快照里**总有** provider 条目（未配置时那一条是
                # ``unavailable``）。写死 ``1 if configured else 0`` 会把"提供方不可用"
                # 报成 ``available``+0 个不可用，让排查者看不到真正的原因。
                providers = tuple(availability.providers)
                available_providers = sum(
                    1 for provider in providers if provider.get("status") == "available"
                )
                unavailable_providers = len(providers) - available_providers
                agent_status = "degraded" if unavailable_providers > 0 else "available"
            snapshot["agents"] = {
                "status": agent_status,
                # 没有提供方时两个计数都是 0：谈不上"不可用的提供方"。
                "availableProviders": available_providers,
                "unavailableProviders": unavailable_providers,
            }

        service_unavailable = (
            database_health["status"] == "unavailable" or not accepting
        )
        # **degraded 与 unavailable 的分界**：后者是"服务不能工作"，前者是"能工作但
        # 有部分能力受损"。Gmail 需重同步、Agent 未配置都属于后者——把它们报成
        # unavailable 会让客户端放弃一个其实可用的服务。
        # **任何非 ``available`` 的 Agent 状态都算降级**——包括 ``unknown``。
        # 参考实现的判定就是 `agentStatus !== "available"`。
        external_degraded = attention_required > 0 or agent_status != "available"
        status = (
            "unavailable" if service_unavailable
            else "degraded" if external_degraded
            else "healthy"
        )
        if status == "unavailable":
            response.status_code = 503

        # ``status`` 放在最前，与参考实现的字段顺序一致（便于人工比对）。
        return {"status": status, **snapshot}

    return router

"""应用工厂与 uvicorn 入口。

设计上把**已打开的数据库**注入应用，而不是在应用内部自行打开：
这样测试可以把应用挂在临时库或由 TypeScript 创建的 schema 15 库上，
不需要触碰部署配置，也不会因为导入模块而产生副作用。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import FastAPI

from .api.agent_events import EventConnectionRegistry, create_agent_events_router
from .api.agent_routes import create_agent_router
from .api.auth_routes import build_owner_login_service, create_auth_router
from .api.error_envelope import install_error_envelope
from .api.gmail_routes import create_gmail_router
from .api.health import create_health_router
from .application.agent_availability import create_agent_availability_snapshot
from .application.write_coordinator import WriteCoordinator
from .infrastructure.db.gmail_connection_repository import load_gmail_connections
from .api.import_routes import create_import_router
from .api.ledger_routes import build_authenticator, create_ledger_router
from .api.market_routes import create_market_router
from .api.system import create_system_router
from .config import Settings
from .db.connection import LyraDatabase, open_lyra_database


async def _authorize_owner_device(
    *,
    identity: object,
    public_key_pem: str,
    display_name: str,
    database: object,
    write_coordinator: object,
) -> object:
    """登录成功后的设备签发。

    **异步**：签发走单写队列，而队列用 ``asyncio.Lock`` 串行化。

    ``write_coordinator`` 由调用方注入**应用级单例**：每个调用点各自
    ``new WriteCoordinator()`` 会拿到各自实例的锁，等于没有队列——健康端点报告的
    队列状态也会与实际写入脱节。
    """
    from .application.device_pairing_service import DevicePairingService
    from .infrastructure.db.device_repository import DuckDbDeviceRepository

    service = DevicePairingService(
        DuckDbDeviceRepository(database.connection), write_coordinator
    )
    issued = await service.authorize_owner_device(
        identity=identity, display_name=display_name, public_key_pem=public_key_pem
    )
    return {
        "deviceId": issued.device_id,
        "displayName": issued.display_name,
        "accessToken": issued.access_token,
        "accessTokenExpiresAt": issued.access_token_expires_at,
        "refreshToken": issued.refresh_token,
        "refreshTokenExpiresAt": issued.refresh_token_expires_at,
    }


def create_app(settings: Settings, database: LyraDatabase) -> FastAPI:
    """构造 FastAPI 应用。

    @param settings: 已校验的环境配置。
    @param database: 已迁移完成的账本连接；生命周期由调用方管理。
    @returns 已挂载全部路由的应用。
    """
    app = FastAPI(
        title="Lyra Server",
        version="0.1.0",
        # 迁移期间不暴露交互式文档：它与 TypeScript 版行为不一致，
        # 且会在协议对齐完成前给出误导性的接口清单。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # **统一错误信封与请求 ID 必须在挂载路由之前装配。**
    install_error_envelope(app)
    # **健康快照需要三段来源**：写队列、Gmail 连接、Agent 可用性。
    # 用 lambda 延迟取值——这些状态在下方才装配，且 Gmail 连接是每请求读取的。
    #
    # Agent 段传**真实可用性快照**而不是布尔：运行时的可用性由 provider 列表表达
    # （未配置时那条是 ``unavailable``），健康端点据此推导计数与状态。
    app.include_router(create_health_router(
        database,
        write_coordinator_provider=lambda: app.state.write_coordinator,
        gmail_connections=lambda: load_gmail_connections(database.connection),
        agent_availability=lambda: create_agent_availability_snapshot(
            configured=app.state.agent_executor is not None,
            checked_at=datetime.now(UTC),
        ),
    ))
    app.include_router(create_system_router(database))
    app.include_router(create_ledger_router())
    app.include_router(create_import_router())
    app.include_router(create_agent_router())
    # 事件连接的注册表：登出时据此踢掉该设备已经建立的事件流。
    app.state.event_connections = EventConnectionRegistry()
    app.include_router(create_agent_events_router())
    app.include_router(create_market_router())
    # 登录服务：按环境装配。部分配置会在启动时抛错——让服务带着"看起来启用了
    # 但永远登不进去"的配置上线是最糟的选择。
    #
    # 设备签发用例指向尚未接线的 `exchange` 路径，见 auth_routes 的模块文档。
    app.state.login_service = build_owner_login_service(
        os.environ,
        lambda **kwargs: _authorize_owner_device(
            database=database,
            # lambda 体在**请求期**求值，晚于下方 `app.state.write_coordinator` 的赋值，
            # 因此这里取到的就是那个单例。
            write_coordinator=app.state.write_coordinator,
            **kwargs,
        ),
    )
    app.state.login_provider_ids = frozenset(
        provider.id for provider in app.state.login_service._providers
    )
    app.include_router(create_auth_router())
    # Gmail 服务：仓储 + 加密器 + OAuth 客户端 + Gmail 客户端的装配。
    #
    # **Gmail 连接加密是 SecretCipher 的第一个消费者**，因此主密钥在这里第一次被使用。
    # 密钥格式非法会在启动时失败（config 的 pattern 已拦截）。
    from .infrastructure.gmail.service_factory import build_gmail_service
    from .security.secret_cipher import SecretCipher, master_key_from_hex

    app.state.gmail_service = build_gmail_service(
        connection=database.connection,
        environment=os.environ,
        cipher=SecretCipher(master_key_from_hex(settings.lyra_secret_master_key)),
    )
    app.include_router(create_gmail_router())
    # **写队列协调器是单一实例**：健康端点的 ``writeQueue`` 段与配对服务必须看到同一个
    # 队列状态——各建一个会让健康报告与实际写入路径脱节。
    app.state.write_coordinator = WriteCoordinator()
    app.state.settings = settings
    app.state.database = database
    app.state.authenticator = build_authenticator(database)
    # 回合编排器：持有会话的进程内运行态（事件缓冲、活跃回合）。
    from .application.agent_turn_orchestrator import AgentTurnOrchestrator
    from .infrastructure.db.agent_turn_store import DuckDbAgentTurnStore

    app.state.agent_orchestrator = AgentTurnOrchestrator(
        store=DuckDbAgentTurnStore(database.connection),
        now=lambda: datetime.now(UTC),
    )
    # 模型执行器：按环境配置装配。
    #
    # **装配失败即启动失败**（配置非法时抛错），而不是延迟到第一次对话——与 TS 侧
    # "任何部分配置都在数据库迁移前失败"一致：让服务以"看起来启用了但每次都失败"
    # 的状态上线是最糟的选择。
    from .application.financial_tool_definitions import build_tool_definitions
    from .infrastructure.agents.genkit_runtime import create_executor_from_environment

    tool_definitions = build_tool_definitions(
        lambda: database.connection,
        # 行情运行时在下方装配；用 lambda 延迟取值，避免顺序耦合。
        lambda: getattr(app.state, "market_runtime", None),
    )
    # 行情运行时：按环境配置装配。未配置任何来源时仍装配一个空运行时，
    # 使设置端点可用（用户可以查看"尚无来源"而不是拿到 500）。
    from .infrastructure.market_data.config import load_market_data_config
    from .infrastructure.market_data.httpx_fetch import httpx_market_fetch
    from .infrastructure.market_data.runtime import create_market_data_runtime

    app.state.market_runtime = create_market_data_runtime(
        load_market_data_config(os.environ),
        fetch=httpx_market_fetch,
        now=lambda: int(datetime.now(UTC).timestamp() * 1000),
    )
    app.state.agent_executor = create_executor_from_environment(
        connection=database.connection,
        environment=os.environ,
        tool_definitions=tool_definitions,
    )
    return app


def run() -> None:
    """按环境配置启动服务。

    读取配置 → 打开并迁移账本 → 交给 uvicorn。配置缺失或迁移失败时进程
    直接退出，不进入「半启动」状态。
    """
    import uvicorn

    settings = Settings()  # type: ignore[call-arg]  # 字段由环境变量提供
    database = open_lyra_database(settings.lyra_database_path)
    try:
        uvicorn.run(
            create_app(settings, database),
            host=settings.lyra_host,
            port=settings.lyra_port,
            log_level="info",
        )
    finally:
        database.close()


if __name__ == "__main__":
    run()

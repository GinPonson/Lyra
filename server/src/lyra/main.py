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
from .api.auth_routes import build_login_service, create_auth_router
from .api.error_envelope import install_error_envelope
from .api.gmail_routes import create_gmail_router
from .api.health import create_health_router
from .application.agent_availability import create_agent_availability_snapshot
from .application.write_coordinator import WriteCoordinator
from .infrastructure.db.invite_code_repository import InviteCodeRepository
from .infrastructure.db.gmail_connection_repository import (
    count_gmail_connections_needing_attention,
)
from .api.import_routes import create_import_router
from .api.ledger_routes import build_authenticator, create_ledger_router
from .api.market_routes import create_market_router
from .api.system import create_system_router
from .config import Settings
from .db.connection import LyraDatabase, open_lyra_database


async def _authorize_user_device(
    *,
    identity: object,
    public_key_pem: str,
    display_name: str,
    invite_code_hash: str | None,
    database: object,
    write_coordinator: object,
) -> object:
    """登录成功后的用户解析与设备签发。

    **异步**：签发走单写队列，而队列用 ``asyncio.Lock`` 串行化。

    ``write_coordinator`` 由调用方注入**应用级单例**：每个调用点各自
    ``new WriteCoordinator()`` 会拿到各自实例的锁，等于没有队列——健康端点报告的
    队列状态也会与实际写入脱节。

    ``user`` 与 ``identity`` 一并返回而不是只返回设备字段：登录响应必须给出与
    ``GET /v1/me`` 同形的 ``user`` 对象（见 P0 契约 §3.9），而它只能来自本次解析出
    的用户与身份。缺这两个 key 会让 ``auth_routes`` 的兑换路径 KeyError。
    """
    from .application.device_pairing_service import DevicePairingService
    from .infrastructure.db.device_repository import DuckDbDeviceRepository
    from .infrastructure.db.invite_code_repository import (
        InviteCodeRepository,
        invite_required_error,
        registration_blocked_error,
    )
    from .infrastructure.db.user_repository import UserRepository

    invites = InviteCodeRepository(database.connection)

    def consume_invite(user_id: str) -> None:
        """注册门禁：新用户必须核销一枚邀请码，否则整体回滚。

        **这里断言"新用户必然带码"是刻意的**：调用方即便漏传，注册也不会被放行。
        """
        if invite_code_hash is None:
            raise invite_required_error()
        if not invites.consume(
            code_hash=invite_code_hash, used_by=user_id, now=datetime.now(UTC)
        ):
            raise registration_blocked_error()

    service = DevicePairingService(
        DuckDbDeviceRepository(database.connection),
        write_coordinator,
        UserRepository(database.connection),
    )
    issued = await service.authorize_user_device(
        identity=identity,
        display_name=display_name,
        public_key_pem=public_key_pem,
        on_new_user=consume_invite,
    )
    return {
        "deviceId": issued.device_id,
        "displayName": issued.display_name,
        "accessToken": issued.access_token,
        "accessTokenExpiresAt": issued.access_token_expires_at,
        "refreshToken": issued.refresh_token,
        "refreshTokenExpiresAt": issued.refresh_token_expires_at,
        "user": issued.user,
        "identity": issued.identity,
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
        # Gmail 段传**部署级聚合数**而不是连接列表：多用户下连接数是用户活动信息，
        # 公开健康响应不得暴露（计划 §4.4）。健康端点只据此推导 available/degraded。
        gmail_attention_provider=lambda: count_gmail_connections_needing_attention(
            database.connection
        ),
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
    app.state.login_service = build_login_service(
        os.environ,
        lambda **kwargs: _authorize_user_device(
            database=database,
            # lambda 体在**请求期**求值，晚于下方 `app.state.write_coordinator` 的赋值，
            # 因此这里取到的就是那个单例。
            write_coordinator=app.state.write_coordinator,
            **kwargs,
        ),
        # ``/start`` 的提前校验与注册时的核销共用同一个仓储。
        InviteCodeRepository(database.connection),
    )
    # ``_providers`` 是 ``dict[provider_id, ConfiguredLoginProvider]``，因此**迭代它得到的是
    # 字符串 key**。原先写成 ``provider.id for provider in ...`` 会对字符串取 ``.id`` 抛
    # ``AttributeError``——这个 bug 一直潜伏，只因为没有来源被配置时空字典让循环体不执行；
    # **任何配齐一组 OAuth 凭据的真实部署都会在启动时直接崩**。
    app.state.login_provider_ids = frozenset(app.state.login_service._providers)
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
    # **启动期不再装载任何"已保存的行情设置"。**
    #
    # 那行代码把某一个用户的设置装成全局运行时偏好，多用户下等于让启动时读取的那
    # 一个用户覆盖所有人的来源选择（计划 §4.4「禁止每个请求覆盖同一 runtime 的
    # 全局路由」）。现在每次请求按自己的 user_id 从库里读快照，运行时不再保存
    # 当前路由与修订号——见 P0 契约 §3.12。
    #
    # 这里保留的 `market_runtime` 仍携带**启动默认路由**与共享的报价服务：供应商
    # 凭据、公开报价缓存与供应商预算保持部署级共享，不随用户划分。
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

"""登录来源目录装配。

镜像 TypeScript 版 ``owner-login-providers.ts``。**先完整验证所有已填写的来源
配置**：任何部分配置都在数据库迁移前失败，避免服务以"看起来启用了但永远登不
进去"的状态启动。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..infrastructure.auth.google_login_client import (
    GoogleLoginClient,
    load_google_login_config,
    validate_google_login_config,
)
from ..infrastructure.auth.google_login_client import (
    assert_configured_owner as assert_google_owner,
)
from ..infrastructure.auth.login_provider import ConfiguredLoginProvider, LoginIdentity
from ..infrastructure.auth.oauth_login_client import (
    OAuthLoginClient,
    assert_oauth_configured_owner,
    load_oauth_login_config,
)


def configured_google_login_provider(
    config: object,
    client: object | None = None,
) -> ConfiguredLoginProvider:
    """把已校验的 Google 配置装配为登录来源。

    @param config: 已校验的 ``GoogleLoginConfig``。
    @param client: 可注入的客户端（测试用）。
    @returns 可进入登录目录的来源。
    """
    validate_google_login_config(config)  # type: ignore[arg-type]
    from ..infrastructure.auth.google_login_client import GoogleLoginConfig

    resolved: GoogleLoginConfig = config  # type: ignore[assignment]

    def assert_owner(identity: LoginIdentity) -> None:
        if identity.provider != "google" or identity.email is None:
            from ..application.app_error import ApplicationError

            raise ApplicationError(
                code="LOGIN_FAILED",
                message="无法确认登录身份",
                status_code=401,
                retryable=False,
            )
        assert_google_owner(
            _as_google_identity(identity), resolved
        )

    return ConfiguredLoginProvider(
        id="google",
        name="Google",
        client=client if client is not None else GoogleLoginClient(resolved),
        assert_owner=assert_owner,
    )


def _as_google_identity(identity: LoginIdentity) -> object:
    """把通用身份收窄为 Google 身份；邮箱缺失时由调用方保证不会走到这里。"""
    from ..infrastructure.auth.google_login_client import GoogleLoginIdentity

    return GoogleLoginIdentity(
        provider="google",
        subject=identity.subject,
        email=identity.email or "",
        display_name=identity.display_name,
    )


def load_owner_login_providers(
    environment: Mapping[str, str | None],
) -> list[ConfiguredLoginProvider]:
    """按固定顺序装配全部已配置的登录来源。

    @param environment: 环境变量映射。
    @returns 已装配的来源；顺序与 ``LOGIN_PROVIDER_IDS`` 一致（google、linuxdo、github）。
    @raises ValueError: 任一已填写来源的配置不完整或非法。
    """
    providers: list[ConfiguredLoginProvider] = []

    google = load_google_login_config(environment)
    if google is not None:
        providers.append(configured_google_login_provider(google))

    for provider_id in ("linuxdo", "github"):
        config = load_oauth_login_config(environment, provider_id)
        if config is None:
            continue
        providers.append(ConfiguredLoginProvider(
            id=provider_id,
            name="GitHub" if provider_id == "github" else "LINUX DO",
            client=OAuthLoginClient(config),
            assert_owner=lambda identity, cfg=config: assert_oauth_configured_owner(identity, cfg),
        ))

    return providers

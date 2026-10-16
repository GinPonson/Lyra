"""登录来源目录装配。

镜像 TypeScript 版 ``owner-login-providers.ts``，但**已去掉 owner 白名单**：多用户下
一个来源是否可用只取决于它的 OAuth 凭据是否配置齐全，谁能登录由登录事务按已验证的
``(provider, subject)`` 解析用户（未注册则自动注册为 ``member``）。

**先完整验证所有已填写的来源配置**：任何部分配置都在数据库迁移前失败，避免服务以
"看起来启用了但永远登不进去"的状态启动。这一步仍然保留——它校验的是 OAuth 凭据与
回调地址，而不是某个人的 subject。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..infrastructure.auth.google_login_client import (
    GoogleLoginClient,
    load_google_login_config,
    validate_google_login_config,
)
from ..infrastructure.auth.login_provider import ConfiguredLoginProvider
from ..infrastructure.auth.oauth_login_client import (
    OAuthLoginClient,
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

    return ConfiguredLoginProvider(
        id="google",
        name="Google",
        client=client if client is not None else GoogleLoginClient(resolved),
    )


def load_login_providers(
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
        ))

    return providers

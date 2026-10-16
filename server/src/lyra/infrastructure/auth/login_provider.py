"""登录来源契约。

镜像 TypeScript 版 ``login-provider.ts``。**三种来源各自绑定不可变的来源主体标识，
不按邮箱或用户名自动关联身份**：同名或同邮箱不会让两个身份落到同一个用户上。

多用户下这里不再有 owner 白名单——来源是否可用由 OAuth 配置决定，身份到用户的映射
由 ``UserRepository`` 按 ``(provider, subject)`` 解析（未注册则自动注册为 ``member``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

LoginProviderId = Literal["google", "linuxdo", "github"]

# 登录目录固定顺序；界面按此顺序展示。
LOGIN_PROVIDER_IDS: tuple[LoginProviderId, ...] = ("google", "linuxdo", "github")

LOGIN_PROVIDER_NAMES: dict[LoginProviderId, str] = {
    "google": "Google",
    "linuxdo": "LINUX DO",
    "github": "GitHub",
}


@dataclass(frozen=True)
class LoginIdentity:
    """只保留验证过的来源身份。

    第三方 access token **永不**进入 App 会话或持久化层，因此这里没有它的位置。
    """

    provider: LoginProviderId
    subject: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class ConfiguredLoginProvider:
    """配置齐全即可进入可用登录目录的来源。

    **没有 ``assert_owner``**：白名单已整体退出运行时鉴权（契约 §3.10）。保留一个
    永不生效的准入钩子会让它看起来仍是一道权限闸门，而实际上谁校验、何时校验都
    不明确——身份到用户的解析必须在登录事务里显式完成。
    """

    id: LoginProviderId
    name: str
    client: Any
